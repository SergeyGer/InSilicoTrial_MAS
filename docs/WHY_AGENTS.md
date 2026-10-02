# Why agents?

A fair question to ask of any system that calls itself multi-agent: **would a
plain simulation loop not do the same job?** This note answers it with the
concrete requirements the platform has to satisfy, the places where the agent
boundary is load-bearing, and — just as important — the places where it is not.

## The short answer

A clinical trial is not one equation, it is **several actors with different
information, different objectives and their own state**: a sponsor who runs the
protocol, participants who react to it, and a statistician who watches the
accumulating data and can stop the study. Modelling those actors as separate
agents is not decoration — it is what makes the information barriers, the
sequential decisions and the fault isolation expressible at all.

## The five requirements the agent boundary carries

### 1. State-dependent decisions across epochs

A participant's behaviour at epoch *n* depends on their own history: exposure so
far, adverse events suffered, adherence, whether they withdrew consent. Titration
in the protocol depends on the *observed* value, not on a planned one.

A closed-form simulator computes a trajectory; it cannot express "reduce the dose
because this individual's creatinine rose at epoch 3". The patient agent owns that
history and returns one observation per epoch
(`PatientStateObservation`), which is exactly the Silver row contract.

### 2. Information barriers that must be enforced, not remembered

Blinding is a *structural* property of a trial:

* a patient agent receives **only** its own history and a `DoseDirective` — it never
  sees other patients, the arm assignment of others, or the aggregate results;
* the Biostatistician Agent sees the full cohort but does not participate in
  running it, so unblinding and analysis are separated by an interface, not by
  convention;
* the Protocol Agent knows allocations but not outcomes.

In a monolithic simulator these boundaries live in the programmer's head, and
look-ahead bias (a patient "knowing" the future, or an analysis peeking at
unblinded data) is one careless variable access away. As agents, the boundary is an
API: passing forbidden information requires writing new code, not forgetting a
rule.

### 3. Interim decisions on partial data

The DSMB rules are evaluated on the cohort as it accumulates
(`stopping_evaluations`, `evaluate_stopping_rules`). Stopping, dose modification
and adaptive escalation are **sequential decision problems**: the action at epoch
*n* is a function of the data available at epoch *n*. Monte Carlo sampling over
final readouts cannot represent them — you need actors that act at each step under
the information they actually have.

### 4. Fault isolation and cost control at cohort scale

Ten thousand to a million agent-epochs run against an external LLM provider.
Something will throttle, time out or return malformed JSON. Because each persona is
an agent, a failure degrades **one observation**: the resilient client retries,
falls back to the deterministic offline provider, and the row records a classified
error kind (`llm_error`) instead of aborting the run. In a single-process pipeline
an unhandled provider error is a failed simulation.

### 5. The same unit of work everywhere

The agent's per-patient step is the unit of parallelism. That is why the identical
agent code runs sequentially, in a process pool, and inside Spark
(`applyInPandas` / `mapInPandas`) and produces **bit-identical** results — each
agent draws from its own seeded stream
(`rng_for(seed, run, patient, epoch, purpose)`), so partitioning cannot change a
number. Without a clean agent boundary, engine portability would mean three
implementations of the science.

## Where agents are *not* needed (and are not used)

Being honest about this is what makes the rest credible:

| Layer | Why it is not an agent |
| --- | --- |
| PK/PD | Closed-form one-compartment kinetics with superposition — a formula, vectorised, thousands of patients per second. |
| Learned biomarker head | A single ridge regression artefact, trained offline, applied as a pure function. |
| Statistics | Wilson/Newcombe/Welch/Fisher/Benjamini-Hochberg — deterministic estimators, no autonomy. |
| Storage and governance | Delta Lake, Unity Catalog: infrastructure, not actors. |
| The LLM | A **component inside** the persona agent (narration of symptoms), not the agent's controller. The medical decisions come from the mechanistic model; the LLM adds qualitative text under a validated JSON contract. |

So the claim is deliberately narrow: agents are the **orchestration and decision
layer**, not the numerics. Roughly 15k lines of the codebase are the simulation and
the platform; the three agent classes are the seam where *who decides what* is
made explicit.

## What would actually break without them

1. Adherence, discontinuation and titration would collapse into post-hoc filtering
   of a trajectory table — losing the causal chain "event → decision → next
   observation".
2. Blinding would become a documentation promise instead of an interface.
3. Interim analyses could not exist, so no DSMB simulation and no adaptive design.
4. One provider outage would end a 10,000-patient run.
5. Adding a role (site/PI agent, regulator agent, market-access agent) would mean
   editing the core loop instead of implementing an interface.

## Is this "agents" in the classical sense?

Yes, in the multi-agent-systems sense rather than the marketing sense: each agent is
**autonomous** (owns its state and loop), **reactive** (responds to directives and
observations), **proactive** (pursues a goal: model this participant's response,
titrate this arm) and **social** (communicates through typed messages —
`DoseDirective`, `PatientStateObservation`, `StoppingRuleEvaluation`). Heterogeneous
roles with typed message passing is the textbook definition of a MAS.

What it is *not*: an LLM-driven autonomous swarm. The policies are mechanistic,
seeded and reproducible; the language model is one component with a narrow,
validated contract. That distinction matters for a regulated audience, and it is
testable: with `llm.provider: offline` the entire platform produces identical
results without any model in the loop.

## The one-paragraph version for a review

> The trial has three actors with different information and different decisions:
> the protocol layer that screens, randomises, doses and titrates; the participants,
> whose next-epoch behaviour depends on their own accumulated history; and the
> statistician, who evaluates interim data against stopping rules. Those are
> concurrent, stateful, message-passing decision-makers — agents. Making them
> separate agents gives us enforced blinding (a patient cannot see the cohort),
> interim analyses on accumulating data, per-agent fault isolation when an LLM call
> fails, and one unit of work that runs unchanged in a process pool or inside Spark
> with bit-identical output. The PK/PD, the ML head and the statistics are ordinary
> deterministic functions — agents were required for the orchestration and the
> decision boundaries, not for the arithmetic.

## The thirty-second version

> Because a trial is not a curve, it is a cast. Each participant's next step depends
> on their own history, the protocol reacts to accumulating data, and the
> statistician has to be blind to what the protocol knows. Those information
> barriers and sequential decisions are the product — they are what a regulator or a
> trialist is actually buying. Agents make them structural; a single simulation loop
> would make them a convention that the next contributor can silently break.
