# Use official Python runtime as base image
FROM python:3.12-slim

# Set working directory in container
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file from root
COPY requirements.txt /app/requirements.txt

# Upgrade pip
RUN pip install --upgrade pip

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ /app/app/
COPY data/ /app/data/
COPY config/ /app/config/
COPY Opus4.6/ /app/Opus4.6/
COPY README.md /app/

# Copy Streamlit config to the working directory where it will be run
COPY .streamlit/ /app/app/.streamlit/

# Create .dockerenv file to indicate container environment
RUN touch /.dockerenv

# Expose Streamlit default port
EXPOSE 8501

# Health check
# start-period must be long enough for Streamlit to boot with heavy imports
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# Set working directory to app folder
WORKDIR /app/app

# Run Streamlit application with Kubernetes-specific overrides
CMD ["streamlit", "run", "streamlit_app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.enableWebsocketCompression=false", \
     "--server.enableXsrfProtection=false", \
     "--browser.serverAddress=localhost"]
