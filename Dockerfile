# Reel Maker for Cloudflare Containers: the same app as `reel ui`, plus FFmpeg.
# Built by .github/workflows/deploy.yml (needs Docker; Cloudflare runs linux/amd64).
FROM python:3.12-slim-bookworm

# Debian's FFmpeg includes zscale (libzimg) for HDR -> SDR tone-mapping and libx264.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NUMBA_CACHE_DIR=/tmp/numba

WORKDIR /app
COPY engine/ /app/engine/
RUN pip install /app/engine \
 && python -c "import librosa, cv2, pillow_heif; from reel_engine import ffmpeg; assert ffmpeg.has_filter('zscale')"

# Uploads, working files and reels. The container's disk is wiped when it sleeps.
RUN useradd --create-home --uid 1000 reel && mkdir /data && chown reel /data
USER reel

EXPOSE 8080
CMD ["reel", "ui", "--host", "0.0.0.0", "--port", "8080", "--no-browser", "--data", "/data"]
