"""Prompt construction and robust parsing of LLM symptom reports.

Two defects in the specification's blueprint are fixed here:

1. ``chain = prompt | llm | JsonOutputParser()`` was used *without* format
   instructions, so the model had no contract to satisfy - a frequent cause of
   ``OutputParserException`` in production. The persona prompt below embeds an
   explicit JSON schema and few-shot example.
2. A parse failure was surfaced as free text in the ``symptoms`` column
   (``"Error capturing response: ..."``), which silently corrupts downstream
   analytics. Here a failure is a structured, typed event: the caller decides to
   retry, to fall back to the offline heuristic, or to flag the row.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..reproducibility import stable_hash
from ..schemas import SymptomReport

SYMPTOM_JSON_SCHEMA = """{
  "symptoms": [
    {"term": "<short clinical term>", "ctcae_grade": <1-5>, "verbatim": "<what the patient says>"}
  ],
  "overall_tolerability": "<good|acceptable|poor>",
  "adherence_intent": "<continue|unsure|discontinue>"
}"""

PATIENT_PERSONA_SYSTEM_PROMPT = (
    "You are a synthetic patient persona inside a validated in-silico clinical trial. "
    "You are NOT a physician and you never give medical advice. "
    "Stay strictly in character: report only symptoms consistent with your phenotype, "
    "your medical history and the exposure described to you. "
    "Answer with a single JSON object and nothing else."
)

PATIENT_PERSONA_TEMPLATE = """Patient profile: Age {age}, Sex {sex}, BMI {bmi}.
Medical history: {history}
Genotype notes: {genotype}
Randomised arm: {arm_label} (dose {dose} mg administered in epoch {epoch}).
Model-predicted physiology this epoch: systolic BP {sbp:.0f} mmHg, diastolic BP {dbp:.0f} mmHg, \
heart rate {hr:.0f} bpm, QTc {qtc:.0f} ms, ALT {alt:.0f} U/L (baseline ALT {baseline_alt:.0f} U/L).
Predicted adverse-event probability of highest-risk term ({top_term}): {top_prob:.0%}.

Describe how you feel after receiving this dose, as JSON matching exactly this schema:
{schema}

Rules:
- 0 to 3 symptoms, most bothersome first.
- CTCAE grade 1 = mild, 2 = moderate, 3 = severe, 4 = life-threatening, 5 = fatal.
- Never invent a symptom that contradicts the physiological data above.
- Output JSON only, no markdown, no commentary."""


@dataclass(slots=True)
class PatientPrompt:
    """A rendered prompt plus the metadata needed for caching and tracing."""

    system: str
    user: str
    prompt_hash: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_messages(self) -> list[dict[str, str]]:
        messages = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        messages.append({"role": "user", "content": self.user})
        return messages


def _genotype_notes(profile) -> str:
    notes = [f"CYP2D6 {profile.genomic.cyp2d6} metaboliser", f"ancestry {profile.genomic.ancestry}"]
    if profile.genomic.hla_b_57_01:
        notes.append("HLA-B*57:01 carrier (hypersensitivity risk)")
    if profile.genomic.slco1b1_decreased:
        notes.append("reduced SLCO1B1 transport function")
    if profile.genomic.adrb1_arg389gly:
        notes.append("ADRB1 Arg389Gly (enhanced beta-blockade response)")
    return "; ".join(notes)


def build_patient_prompt(
    profile,
    *,
    arm_label: str,
    dose_mg: float,
    epoch: int,
    sbp: float,
    dbp: float,
    hr: float,
    qtc_ms: float,
    alt_u_l: float,
    ae_probabilities: dict[str, float],
) -> PatientPrompt:
    """Render the persona prompt for one patient-epoch."""
    top_term = "none"
    top_prob = 0.0
    if ae_probabilities:
        top_term, top_prob = max(ae_probabilities.items(), key=lambda kv: kv[1])
    user = PATIENT_PERSONA_TEMPLATE.format(
        age=int(profile.age),
        sex="female" if profile.sex == "F" else "male",
        bmi=profile.bmi,
        history=profile.persona_text(),
        genotype=_genotype_notes(profile),
        arm_label=arm_label,
        dose=dose_mg,
        epoch=epoch,
        sbp=sbp,
        dbp=dbp,
        hr=hr,
        qtc=qtc_ms,
        alt=alt_u_l,
        baseline_alt=profile.alt_u_l,
        top_term=top_term,
        top_prob=top_prob,
        schema=SYMPTOM_JSON_SCHEMA,
    )
    prompt_hash = stable_hash(PATIENT_PERSONA_SYSTEM_PROMPT, user, length=16)
    return PatientPrompt(
        system=PATIENT_PERSONA_SYSTEM_PROMPT,
        user=user,
        prompt_hash=prompt_hash,
        context={
            "patient_id": profile.patient_id,
            "age": profile.age,
            "sex": profile.sex,
            "arm_label": arm_label,
            "dose_mg": dose_mg,
            "epoch": epoch,
            "ae_probabilities": ae_probabilities,
            "predicted": {"sbp": sbp, "dbp": dbp, "hr": hr, "qtc_ms": qtc_ms, "alt_u_l": alt_u_l},
        },
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SINGLE_QUOTE_KEY_RE = re.compile(r"'([^'\\]*)'\s*:")

VALID_TOLERABILITY = {"good", "acceptable", "poor"}
VALID_ADHERENCE = {"continue", "unsure", "discontinue"}

#: Guard-rail: symptom terms that must never be produced (the LLM is not a doctor).
FORBIDDEN_MEDICAL_ADVICE = (
    "you should take",
    "i recommend",
    "diagnosis is",
    "prescribe",
    "stop taking your",
)


@dataclass(slots=True)
class ParsedSymptoms:
    """Result of parsing one LLM answer."""

    symptoms: list[SymptomReport]
    tolerability: str = "acceptable"
    adherence_intent: str = "continue"
    parse_error: str = ""
    repaired: bool = False
    raw_text: str = ""

    @property
    def ok(self) -> bool:
        return not self.parse_error


def _strip_fences(text: str) -> tuple[str, bool]:
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip(), True
    return text.strip(), False


def _repair_json(text: str) -> str:
    """Apply conservative repairs for the most common LLM JSON mistakes."""
    repaired = _TRAILING_COMMA_RE.sub(r"\1", text)
    repaired = _SINGLE_QUOTE_KEY_RE.sub(r'"\1":', repaired)
    repaired = repaired.replace("True", "true").replace("False", "false").replace("None", "null")
    return repaired


def _extract_payload(text: str) -> tuple[dict[str, Any], bool]:
    """Extract the first JSON object (or array) from a free-text answer.

    Handles the four shapes models actually produce: strict JSON, fenced JSON,
    JSON with trailing commas/quotes, and Python-literal dicts (``{'term': 'x'}``)
    which are parsed with :func:`ast.literal_eval` - never ``eval``.
    """
    candidate, repaired = _strip_fences(text)
    if not candidate:
        raise ValueError("empty response")

    payload: Any = None
    failure: Exception | None = None
    for extractor in (lambda text: text, _first_object, _first_array):
        raw = extractor(candidate)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            break
        except json.JSONDecodeError as strict_error:
            # Expected for the shapes models actually emit: fenced JSON, trailing
            # commas, single quotes. Fall through to the repair strategies below and
            # remember why, so a total failure can report the original cause.
            failure = strict_error
        try:
            payload = json.loads(_repair_json(raw))
            repaired = True
            break
        except json.JSONDecodeError as repair_error:
            failure = repair_error
        try:
            literal = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            continue
        if isinstance(literal, (dict, list)):
            payload = literal
            repaired = True
            break

    if payload is None:
        raise ValueError(f"no JSON object found in the response ({failure})")
    if isinstance(payload, list):  # tolerate a bare array of symptoms
        payload = {"symptoms": payload}
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    # A bare symptom object ({"term": ..., "grade": ...}) is also accepted.
    if "symptoms" not in payload and any(key in payload for key in ("term", "symptom", "name")):
        payload = {"symptoms": [payload]}
    return payload, repaired


def _first_object(text: str) -> str:
    match = _JSON_OBJECT_RE.search(text)
    return match.group(0) if match else ""


def _first_array(text: str) -> str:
    match = _JSON_ARRAY_RE.search(text)
    return match.group(0) if match else ""


def _coerce_grade(value: Any) -> int:
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return int(min(5, max(1, round(float(value)))))
    text = str(value).strip().lower()
    words = {"mild": 1, "moderate": 2, "severe": 3, "life-threatening": 4, "life threatening": 4, "fatal": 5, "death": 5}
    if text in words:
        return words[text]
    match = re.search(r"[1-5]", text)
    return int(match.group(0)) if match else 1


def parse_symptom_response(text: str, *, source: str = "llm") -> ParsedSymptoms:
    """Parse and validate an LLM answer into :class:`SymptomReport` objects.

    Never raises: every failure mode is reported through ``parse_error`` so the
    caller can apply its own policy (retry / fallback / flag).
    """
    if not text or not text.strip():
        return ParsedSymptoms(symptoms=[], parse_error="empty response", raw_text=text)
    lowered = text.lower()
    if any(marker in lowered for marker in FORBIDDEN_MEDICAL_ADVICE):
        return ParsedSymptoms(
            symptoms=[],
            parse_error="response contained medical advice and was rejected by the safety filter",
            raw_text=text,
        )
    try:
        payload, repaired = _extract_payload(text)
    except (ValueError, json.JSONDecodeError) as exc:
        return ParsedSymptoms(symptoms=[], parse_error=f"unparseable LLM response: {exc}", raw_text=text)

    raw_symptoms = payload.get("symptoms", [])
    if isinstance(raw_symptoms, dict):
        raw_symptoms = [raw_symptoms]
    if not isinstance(raw_symptoms, list):
        return ParsedSymptoms(symptoms=[], parse_error="'symptoms' must be a list", raw_text=text)

    symptoms: list[SymptomReport] = []
    for item in raw_symptoms[:5]:
        if isinstance(item, str):
            symptoms.append(SymptomReport(term=item.strip()[:120], ctcae_grade=1, verbatim=item.strip()[:400], source=source))  # type: ignore[arg-type]
            continue
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or item.get("symptom") or item.get("name") or "").strip()
        if not term:
            continue
        verbatim = str(item.get("verbatim") or item.get("quote") or item.get("description") or "").strip()
        symptoms.append(
            SymptomReport(
                term=term[:120],
                ctcae_grade=_coerce_grade(item.get("ctcae_grade", item.get("grade", 1))),
                verbatim=verbatim[:400],
                source=source,  # type: ignore[arg-type]
            )
        )

    tolerability = str(payload.get("overall_tolerability", "acceptable")).strip().lower()
    adherence = str(payload.get("adherence_intent", "continue")).strip().lower()
    return ParsedSymptoms(
        symptoms=symptoms,
        tolerability=tolerability if tolerability in VALID_TOLERABILITY else "acceptable",
        adherence_intent=adherence if adherence in VALID_ADHERENCE else "continue",
        repaired=repaired,
        raw_text=text,
    )


def heuristic_symptom_response(
    ae_probabilities: dict[str, float],
    *,
    epoch: int,
    worst_grade: int,
    threshold: float = 0.15,
) -> str:
    """Deterministic offline stand-in for the LLM (documented, non-clinical).

    Used by the ``offline`` provider and as the fallback when a real provider
    fails. It produces the *same JSON contract* as a real model, so downstream
    code paths are identical and the simulation stays reproducible.
    """
    symptoms: list[dict[str, Any]] = []
    for term, probability in sorted(ae_probabilities.items(), key=lambda kv: -kv[1]):
        if probability < threshold or len(symptoms) >= 3:
            continue
        grade = max(1, min(5, worst_grade if term == "headache" else max(1, worst_grade)))
        verbatim = {
            "headache": "I have a dull headache that started a few hours after the dose.",
            "dizziness": "I feel light-headed when I stand up quickly.",
            "fatigue": "I am more tired than usual today.",
            "nausea": "My stomach feels unsettled and I have little appetite.",
            "rash": "I noticed a red itchy patch on my forearm.",
            "diarrhoea": "I have had loose stools twice today.",
            "peripheral_oedema": "My ankles look a bit puffy this evening.",
            "hyperkalaemia": "I feel muscle weakness and my heart feels irregular.",
            "acute_kidney_injury": "I am passing much less urine than usual.",
            "hepatotoxicity": "I feel nauseated and my right upper abdomen is uncomfortable.",
        }.get(term, f"I have some {term.replace('_', ' ')} since the dose.")
        symptoms.append({"term": term, "ctcae_grade": grade, "verbatim": verbatim})
    payload = {
        "symptoms": symptoms,
        "overall_tolerability": "good" if not symptoms else ("acceptable" if worst_grade <= 2 else "poor"),
        "adherence_intent": "discontinue" if worst_grade >= 4 else ("unsure" if worst_grade == 3 else "continue"),
        "epoch": epoch,
        "source": "offline-heuristic",
    }
    return json.dumps(payload, ensure_ascii=False)
