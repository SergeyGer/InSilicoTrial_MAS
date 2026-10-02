"""Render the GitHub social preview image (1280x640 PNG).

GitHub generates the Open Graph card for a repository from a single uploaded
image, and there is no API for it - the file has to be uploaded by hand in
Settings -> Social preview. This script produces exactly that file, so the card
can be regenerated whenever the positioning changes instead of being an
untraceable binary.

The composition mirrors the product's own design system (dark slate, one clinical
accent, tabular figures) and shows a miniature of what the platform outputs: a
dose-response chart with confidence intervals and a forest plot.

Usage::

    python scripts/render_social_preview.py           # writes .github/social-preview.png
    python scripts/render_social_preview.py --check   # fail if the file is missing
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported lazily so --check needs no dependency
    from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / ".github" / "social-preview.png"

WIDTH, HEIGHT = 1280, 640

BG_TOP = (13, 20, 27)
BG_BOTTOM = (18, 30, 41)
INK = (232, 238, 244)
MUTED = (159, 176, 191)
FAINT = (115, 134, 153)
ACCENT = (74, 168, 208)
VIOLET = (165, 143, 214)
GREEN = (79, 191, 139)
AMBER = (224, 179, 74)
CARD = (23, 33, 43)
CARD_LINE = (38, 52, 64)

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
FONT_REGULAR = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
FONT_MONO = FONT_DIR / "DejaVuSansMono.ttf"


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    """Load a font, falling back to Pillow's default if the system lacks it."""
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(path), size)
    except OSError:  # pragma: no cover - minimal container without DejaVu
        return ImageFont.load_default(size)


def png_size(path: Path) -> tuple[int, int]:
    """Read width/height straight from the PNG IHDR chunk.

    Avoids importing Pillow (and NumPy) just to validate the committed artefact,
    so the CI freshness check stays dependency-free.
    """
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def background() -> Image.Image:
    """Vertical gradient with a soft diagonal accent wash in the top-right."""
    import numpy as np
    canvas = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
    ramp = np.linspace(0.0, 1.0, HEIGHT, dtype=np.float32)[:, None]
    for channel in range(3):
        canvas[:, :, channel] = BG_TOP[channel] * (1 - ramp) + BG_BOTTOM[channel] * ramp

    ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float32)
    glow = np.clip(1.0 - (xs / WIDTH * 0.85 + (HEIGHT - ys) / HEIGHT * 0.55), 0.0, 1.0) ** 2.2
    for channel, strength in enumerate((0.10, 0.22, 0.30)):
        canvas[:, :, channel] = np.clip(canvas[:, :, channel] + glow * strength * 255 * 0.32, 0, 255)

    return Image.fromarray(canvas.astype(np.uint8), "RGB")


def rounded(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    *,
    fill: tuple[int, ...] | None = None,
    outline: tuple[int, ...] | None = None,
    width: int = 1,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def chip(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    label: str,
    *,
    ink: tuple[int, int, int] = MUTED,
    outline: tuple[int, int, int] = CARD_LINE,
    size: int = 15,
) -> int:
    """Draw a pill-shaped chip and return the x coordinate after it."""
    fnt = font(FONT_REGULAR, size)
    pad_x, pad_y = 14, 7
    text_w = draw.textlength(label, font=fnt)
    w = int(text_w + pad_x * 2)
    h = size + pad_y * 2
    rounded(draw, (x, y, x + w, y + h), radius=h // 2, fill=CARD, outline=outline, width=1)
    draw.text((x + pad_x, y + pad_y - 1), label, font=fnt, fill=ink)
    return x + w + 10


def draw_chart_motif(image: Image.Image) -> None:
    """Miniature dose-response chart with confidence intervals.

    Negative changes (a blood-pressure reduction) are drawn downwards from the
    zero line, matching the dashboard's convention so the preview cannot be
    misread as "treatment raises blood pressure".
    """
    draw = ImageDraw.Draw(image, "RGBA")
    left, top, right, bottom = 716, 140, 1240, 458
    rounded(draw, (left - 24, top - 40, right + 20, bottom + 26), 16, fill=(19, 28, 37, 235), outline=CARD_LINE, width=1)

    draw.text((left - 8, top - 30), "PRIMARY ENDPOINT", font=font(FONT_REGULAR, 13), fill=FAINT)
    draw.text((left - 8, top - 11), "systolic BP change (mmHg)", font=font(FONT_REGULAR, 13), fill=MUTED)
    draw.text((right - 8 - draw.textlength("p < 0.0001", font=font(FONT_MONO, 14)), top - 30),
              "p < 0.0001", font=font(FONT_MONO, 14), fill=GREEN)

    plot_top, plot_bottom = top + 22, bottom - 28
    zero_y = plot_top + (plot_bottom - plot_top) * 0.24
    scale = (plot_bottom - zero_y) / 13.5

    for index in range(4):
        y = plot_top + (plot_bottom - plot_top) * index / 3
        draw.line((left, y, right, y), fill=(31, 43, 54), width=1)
    draw.line((left, zero_y, right, zero_y), fill=(72, 92, 110), width=1)
    for tick in (0, -4, -8, -12):
        y = zero_y + abs(tick) * scale
        if y > plot_bottom:
            continue
        draw.line((left, y, left + 6, y), fill=(60, 78, 94), width=1)
        label = str(tick)
        draw.text((left - 10 - draw.textlength(label, font=font(FONT_REGULAR, 12)), y - 7), label,
                  font=font(FONT_REGULAR, 12), fill=FAINT)

    bars = (
        ("placebo", -3.1, -3.8, -2.4, (127, 179, 204)),
        ("low dose", -7.4, -8.2, -6.6, ACCENT),
        ("high dose", -11.7, -12.7, -10.7, VIOLET),
    )
    slot = (right - left) / len(bars)
    bar_w = 54
    for index, (label, _value, low, high, colour) in enumerate(bars):
        cx = left + slot * (index + 0.5)
        y_low = zero_y + abs(low) * scale
        y_high = zero_y + abs(high) * scale
        rounded(draw, (int(cx - bar_w / 2), int(zero_y), int(cx + bar_w / 2), int(y_low)), 5, fill=(*colour, 240))
        draw.line((cx, y_low, cx, y_high), fill=(206, 220, 232, 230), width=2)
        for y in (y_low, y_high):
            draw.line((cx - 9, y, cx + 9, y), fill=(206, 220, 232, 230), width=2)
        label_w = draw.textlength(label, font=font(FONT_REGULAR, 14))
        draw.text((cx - label_w / 2, plot_bottom + 12), label, font=font(FONT_REGULAR, 14), fill=MUTED)

    caption = "synthetic cohort  ·  n = 329  ·  95% CI"
    draw.text((right - 8 - draw.textlength(caption, font=font(FONT_REGULAR, 12)), bottom + 6), caption,
              font=font(FONT_REGULAR, 12), fill=FAINT)


def build() -> Image.Image:
    from PIL import ImageDraw

    image = background()
    draw = ImageDraw.Draw(image, "RGBA")

    # Top accent rule.
    draw.rectangle((0, 0, WIDTH, 4), fill=(*ACCENT, 255))

    # Wordmark.
    mark_box = (64, 60, 118, 114)
    rounded(draw, mark_box, 14, fill=(*ACCENT, 255))
    draw.text((78, 72), "IS", font=font(FONT_BOLD, 26), fill=(255, 255, 255))
    draw.text((134, 66), "InSilicoTrial MAS", font=font(FONT_BOLD, 42), fill=INK)
    draw.text((136, 116), "multi-agent in-silico clinical trial simulation", font=font(FONT_REGULAR, 19), fill=ACCENT)

    # Value proposition.
    draw.text((64, 172), "Simulate a synthetic patient cohort against a trial", font=font(FONT_REGULAR, 23), fill=INK)
    draw.text((64, 202), "protocol before the first human dose: exposure,", font=font(FONT_REGULAR, 23), fill=INK)
    draw.text((64, 232), "efficacy, adverse events and a go/no-go readout.", font=font(FONT_REGULAR, 23), fill=INK)

    # Stack chips.
    x = 64
    for label in ("PySpark", "Delta Lake", "MLflow", "Unity Catalog"):
        x = chip(draw, x, 288, label, size=15)
    x = 64
    for label in ("LangChain / Bedrock", "Terraform", "Databricks", "AWS"):
        x = chip(draw, x, 330, label, size=15)

    # Stat strip.
    stats = (
        ("3", "agent types"),
        ("3", "execution engines"),
        ("10,000", "patients per run"),
        ("206", "tests passing"),
    )
    strip_top = 400
    rounded(draw, (64, strip_top, 620, strip_top + 96), 14, fill=CARD, outline=CARD_LINE, width=1)
    slot = (620 - 64) / len(stats)
    for index, (value, label) in enumerate(stats):
        cx = 64 + slot * (index + 0.5)
        draw.text((cx - draw.textlength(value, font=font(FONT_BOLD, 27)) / 2, strip_top + 22),
                  value, font=font(FONT_BOLD, 27), fill=INK)
        draw.text((cx - draw.textlength(label, font=font(FONT_REGULAR, 13)) / 2, strip_top + 60),
                  label, font=font(FONT_REGULAR, 13), fill=FAINT)

    # Differentiator.
    draw.text((64, 528), "Verified: Spark reproduces the sequential engine bit-for-bit", font=font(FONT_REGULAR, 17), fill=GREEN)
    draw.text((64, 556), "github.com/SergeyGer/InSilicoTrial_MAS", font=font(FONT_MONO, 15), fill=FAINT)

    draw_chart_motif(image)
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description="Render .github/social-preview.png (1280x640)")
    parser.add_argument("--check", action="store_true", help="fail if the image is missing")
    args = parser.parse_args()

    if args.check:
        if not OUTPUT.exists():
            print(f"missing social preview: {OUTPUT.relative_to(REPO_ROOT)}", file=sys.stderr)
            return 1
        size = png_size(OUTPUT)
        if size != (WIDTH, HEIGHT):
            print(f"social preview must be {WIDTH}x{HEIGHT}, found {size}", file=sys.stderr)
            return 1
        print(f"social preview is present and correctly sized ({size[0]}x{size[1]})")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image = build()
    image.save(OUTPUT, format="PNG", optimize=True)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({OUTPUT.stat().st_size / 1024:.0f} KB, {WIDTH}x{HEIGHT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
