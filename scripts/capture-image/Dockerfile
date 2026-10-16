# Throwaway image used to capture the README screenshots and the product tour.
#
# Chromium from the distribution plus Playwright to drive it: this keeps the image
# small (about 1.4 GB) and avoids downloading Playwright's own browser bundle. It is
# only needed by `scripts/capture_media.py`, never by the application itself, so the
# project's own Dockerfile stays free of capture tooling.
#
# Build and use (see docs/DOCKER.md and `make media-capture`):
#
#   docker build -t insilico-capture scripts/capture-image
#   docker run --rm --network host -v "$PWD:/work" -w /work insilico-capture \
#       python scripts/capture_media.py --dashboard <file://…> --studio http://127.0.0.1:8765 --encode
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium \
      fonts-dejavu-core \
      fonts-liberation \
      fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# Playwright drives the system Chromium; its bundled browser download is skipped.
RUN pip install --no-cache-dir playwright==1.49.0
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /work
CMD ["python", "--version"]
