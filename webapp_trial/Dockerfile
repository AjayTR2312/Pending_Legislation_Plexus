# syntax=docker/dockerfile:1
#
# Single-container image for the Plexus (StreamLit template) deployment.
#
# Despite the template's name it hosts any single-container app that serves HTTP
# on one port. Here that is the FastAPI backend, which also serves the built
# React UI as static files, with the conversion worker(s) running alongside it
# (see entrypoint.sh).
#
# BUILD CONTEXT MUST BE THE REPOSITORY ROOT, because the backend imports the
# project's app/ and Opus4.6/ modules:
#
#   docker build -f webapp_trial/Dockerfile -t pending-legislation-webapp .
#
# -----------------------------------------------------------------------------
# Stage 1 — build the React frontend to static files
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/node:20 AS frontend
WORKDIR /build
COPY webapp_trial/frontend/package.json webapp_trial/frontend/package-lock.json ./
RUN npm ci
COPY webapp_trial/frontend/ ./
# BASE_PATH lets the UI be built for a sub-path if the Plexus ingress does NOT
# strip the /<asset>/<prefix> prefix. Default "/" works when it does strip it.
ARG BASE_PATH=/
RUN npm run build -- --base="${BASE_PATH}"

# -----------------------------------------------------------------------------
# Stage 2 — Python runtime
# -----------------------------------------------------------------------------
# Full (non-slim) image: matches the proven TR deployment and ships the shared
# libraries PyMuPDF / OpenCV / NumPy need, reducing missing-.so surprises.
FROM public.ecr.aws/docker/library/python:3.12 AS runtime

# System libraries the scientific/PDF stack needs at runtime.
#   libgomp1      - OpenMP runtime for NumPy / OpenCV
#   libglib2.0-0  - required by opencv-python-headless
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libgomp1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (Plexus runs containers unprivileged).
RUN groupadd -g 1234 deploymentgroup \
    && useradd -m -u 1234 -g deploymentgroup deployment

WORKDIR /workspace

# Install Python dependencies first for better layer caching.
COPY requirements.txt ./requirements.txt
COPY webapp_trial/backend/requirements-plexus.txt ./webapp_trial/backend/requirements-plexus.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
                                   -r webapp_trial/backend/requirements-plexus.txt

# Application code: the backend imports these sibling packages at runtime.
COPY app/ ./app/
COPY Opus4.6/ ./Opus4.6/
COPY webapp_trial/ ./webapp_trial/

# Drop in the compiled frontend so FastAPI's StaticFiles mount can serve it.
COPY --from=frontend /build/dist ./webapp_trial/frontend/dist

RUN chmod +x webapp_trial/entrypoint.sh \
    && chown -R deployment:deploymentgroup /workspace

USER deployment
ENV PYTHONUNBUFFERED=1 \
    PATH="/home/deployment/.local/bin:$PATH" \
    PORT=8501 \
    WORKER_CONCURRENCY=1

EXPOSE 8501

# Health endpoint the platform / load balancer can probe. A longer start period
# accounts for the heavy first-import of the conversion engine (PyMuPDF, Opus).
HEALTHCHECK --interval=30s --timeout=10s --start-period=80s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

ENTRYPOINT ["./webapp_trial/entrypoint.sh"]
