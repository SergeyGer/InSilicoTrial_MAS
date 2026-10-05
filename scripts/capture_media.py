"""Capture the screenshots and the product tour used in the README and the wiki.

The images are *generated from the running application*, so they cannot drift away
from the interface: run a trial, start the Studio, point this script at both, and
the screenshots and the video are rebuilt in about a minute.

    # 1. a run whose readout is worth photographing
    insilico-trial demo --patients 400 --epochs 6 --output-dir artifacts/media

    # 2. the live Studio
    insilico-trial studio --host 0.0.0.0 --port 8765 --no-browser &

    # 3. capture (needs the capture image, see docs/DOCKER.md)
    docker run --rm --network host -v "$PWD:/work" -w /work insilico-capture \\
        python scripts/capture_media.py \\
        --dashboard "file:///work/artifacts/media/<RUN>/report/dashboard.html" \\
        --studio http://127.0.0.1:8765 --encode

Outputs ``docs/images/*.png`` (screenshots at 2x for crisp text) and
``docs/media/demo-tour.mp4`` plus a GIF for the README. Encoding happens with the
ffmpeg binary that ships with ``imageio-ffmpeg``, so no system ffmpeg is required.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS = ("overview", "efficacy", "safety", "patients", "reproducibility", "lineage")
VIEWPORT = {"width": 1600, "height": 1000}
VIDEO_VIEWPORT = {"width": 1280, "height": 720}
CHROME = "/usr/bin/chromium"  # the Debian package inside the capture image


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #
def _shoot(page, path: Path, *, full_page: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=full_page)
    print(f"    {path.name} ({path.stat().st_size // 1024} KB)")


def capture_dashboard(page, url: str, out: Path, frames: Path) -> None:
    print("  dashboard screens")
    page.set_viewport_size(VIEWPORT)
    page.goto(url, wait_until="load")
    page.wait_for_timeout(900)

    for panel in PANELS:
        page.click(f'button[data-tab="{panel}"]')
        page.wait_for_timeout(450)
        _shoot(page, out / f"dashboard-{panel}.png")

    page.click('button[data-tab="overview"]')
    page.wait_for_timeout(600)
    _shoot(page, out / "dashboard-full.png", full_page=True)

    print("  dashboard tour frames")
    frames.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size(VIDEO_VIEWPORT)
    page.click('button[data-tab="overview"]')
    page.wait_for_timeout(400)
    for index in range(12):
        page.mouse.wheel(0, 120)
        page.wait_for_timeout(120)
        page.screenshot(path=str(frames / f"dash-{index:03d}.png"))
    page.click('button[data-tab="efficacy"]')
    page.wait_for_timeout(400)
    for index in range(12, 22):
        page.mouse.wheel(0, 110)
        page.wait_for_timeout(120)
        page.screenshot(path=str(frames / f"dash-{index:03d}.png"))


def capture_studio(page, url: str, out: Path, frames: Path) -> None:
    print("  studio screens")
    page.set_viewport_size(VIEWPORT)
    page.goto(url, wait_until="load")
    page.wait_for_timeout(1200)
    _shoot(page, out / "studio-launcher.png")

    print("  studio tour frames")
    frames.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size(VIDEO_VIEWPORT)
    page.goto(url, wait_until="load")
    page.wait_for_timeout(900)

    try:
        page.select_option("#protocol", index=0)
        page.wait_for_timeout(300)
        page.fill("#epochs", "4")
        page.fill("#seed", "20260101")
        page.wait_for_timeout(500)
    except Exception as exc:  # the form is context, not the point of the tour
        print(f"    (form not scripted: {type(exc).__name__})")

    count = 0

    def frame(tag: str) -> None:
        nonlocal count
        page.screenshot(path=str(frames / f"studio-{count:03d}-{tag}.png"))
        count += 1

    frame("ready")
    page.click("#submit-run")
    for _ in range(28):
        page.wait_for_timeout(700)
        frame("running")
        status = page.query_selector("#form-status")
        text = (status.inner_text() if status else "") or ""
        if "complete" in text.lower() or "dashboard" in text.lower():
            break
    page.wait_for_timeout(1500)
    for _ in range(10):
        page.wait_for_timeout(400)
        frame("result")
    print(f"    {count} frames")


def capture(dashboard: str, studio: str, images: Path, frames: Path, only: str | None) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launch: dict[str, object] = {
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--force-color-profile=srgb",
                     "--font-render-hinting=none", "--hide-scrollbars"],
        }
        if Path(CHROME).exists():
            launch["executable_path"] = CHROME
        browser = playwright.chromium.launch(**launch)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,   # retina screenshots: text stays sharp when scaled
            color_scheme="dark",     # the product's default theme
            locale="en-GB",
        )
        page = context.new_page()
        if only != "studio":
            capture_dashboard(page, dashboard, images, frames)
        if only != "dashboard":
            capture_studio(page, studio, images, frames)
        context.close()
        browser.close()
    return 0


# --------------------------------------------------------------------------- #
# encode
# --------------------------------------------------------------------------- #
def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - encoding is optional
        found = shutil.which("ffmpeg")
        if not found:
            raise SystemExit("install imageio-ffmpeg (pip install imageio-ffmpeg) or ffmpeg") from exc
        return found
    return imageio_ffmpeg.get_ffmpeg_exe()


def _concat_list(frames: Path, pattern: str, target: Path) -> bool:
    files = sorted(frames.glob(pattern))
    if not files:
        return False
    target.write_text("".join(f"file '{path}'\n" for path in files), encoding="utf-8")
    return True


def encode(frames: Path, media: Path, *, fps: int = 8) -> int:
    ffmpeg = _ffmpeg()
    media.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []

    for name, pattern in (("studio", "studio-*.png"), ("dashboard", "dash-*.png")):
        listing = Path(f"/tmp/insilico-{name}.txt")
        if not _concat_list(frames, pattern, listing):
            print(f"  no frames for {name}, skipping")
            continue
        part = Path(f"/tmp/insilico-{name}.mp4")
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-r", str(fps),
             "-i", str(listing), "-vf", "scale=1280:720:flags=lanczos,format=yuv420p",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", str(part)],
            check=True,
        )
        parts.append(part)

    if not parts:
        raise SystemExit("no frames were captured")

    joined = Path("/tmp/insilico-tour.txt")
    joined.write_text("".join(f"file '{path}'\n" for path in parts), encoding="utf-8")
    video = media / "demo-tour.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(joined),
         "-c", "copy", str(video)],
        check=True,
    )
    print(f"    {video.name} ({video.stat().st_size // 1024} KB)")

    gif = media / "demo-tour.gif"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-vf",
         f"fps={fps},scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160[p];"
         f"[b][p]paletteuse=dither=bayer:bayer_scale=3", str(gif)],
        check=True,
    )
    print(f"    {gif.name} ({gif.stat().st_size // 1024} KB)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the README screenshots and product tour")
    parser.add_argument("--dashboard", help="file:// URL of a generated dashboard.html")
    parser.add_argument("--studio", help="base URL of a running Studio")
    parser.add_argument("--images", default=str(REPO_ROOT / "docs" / "images"))
    parser.add_argument("--frames", default=str(REPO_ROOT / ".toolchain" / "frames"))
    parser.add_argument("--only", choices=("dashboard", "studio"))
    parser.add_argument("--encode", action="store_true", help="encode mp4/gif after capturing")
    parser.add_argument("--encode-only", action="store_true", help="skip capture, just encode frames")
    args = parser.parse_args()

    frames = Path(args.frames)
    if not args.encode_only:
        if not args.dashboard or not args.studio:
            parser.error("--dashboard and --studio are required unless --encode-only is used")
        capture(args.dashboard, args.studio, Path(args.images), frames, args.only)

    if args.encode or args.encode_only:
        return encode(frames, REPO_ROOT / "docs" / "media")
    return 0


if __name__ == "__main__":
    sys.exit(main())
