# Roster image — the TECH vertical (deep-tech research Q&A). ONE image, TWO roles (deploy/start.sh
# dispatches on ROSTER_ROLE): the API service serves; the separate ingest-WORKER service runs the corpus
# drain (full-text HTML + DOCLING/torch PDF parsing). docling is a cached layer, so API code pushes
# don't rebuild it, and it's imported lazily so serving never loads torch. No Node/ffmpeg video add-on.
FROM python:3.13-slim

WORKDIR /app

# ca-certificates/curl for TLS + healthcheck; the libgl/libxcb/glib cluster is docling's RUNTIME system
# deps — python:3.13-slim omits them, so docling's PDF layout/table backend fails at load with
# "libxcb.so.1: cannot open shared object file" and silently degrades old (no-HTML) papers to abstract
# only (and a worker re-ingest could clean-replace a recovered full-text back down). libgomp1 = OpenMP
# for torch/onnx. Without these, full-text PDF parsing is broken on prod even though docling imports.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
      libgl1 libglib2.0-0 libxcb1 libxrender1 libsm6 libxext6 libgomp1 \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

# Light, code-independent deps FIRST so their layer caches across code pushes:
# image decode (vision uploads), a light PDF reader for attachments, numpy, and boto3 for R2.
RUN pip install --no-cache-dir Pillow PyMuPDF numpy boto3

# DOCLING — full-text PDF-fallback parser for the WORKER role (arXiv HTML covers most papers with no
# heavy dep). CPU-only torch; own cached layer so code pushes don't rebuild it; imported lazily so the
# API role never loads it.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu docling


# Install the kernel (serve + postgres extras) and the TECH vertical.
COPY packages packages
COPY apps apps
COPY deploy deploy
# committed ingest/maintenance engines (run in-container via `railway ssh`)
COPY scripts scripts
RUN pip install --no-cache-dir "./packages/kernel[serve,postgres]" ./packages/vertical_roster

# apps/ is not a pip package; put it on the path. Single vertical per deployment.
ENV PYTHONPATH=/app/apps \
    ROSTER_ACTIVE_VERTICAL=roster \
    ROSTER_PROVIDER_MODE=live \
    ROSTER_STRUCTURED_ANSWERS=true \
    ROSTER_VIDEO_ENABLED=false \
    ROSTER_GAP_HEALING=true \
    ROSTER_CONVERSATION=true \
    ROSTER_STREAM=true \
    PORT=8000

EXPOSE 8000
CMD ["bash", "deploy/start.sh"]
