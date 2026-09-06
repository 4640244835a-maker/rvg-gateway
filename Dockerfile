# ==========================================
# RVG Gateway Dockerfile
# Production-ready, lightweight multi-protocol proxy container
# ==========================================

FROM python:3.10-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# ==============================================================================
# Persistent Storage / Volumes
# ==============================================================================
# NOTE: The Dockerfile `VOLUME` instruction is intentionally NOT used here.
# Railway buildpacks reject `VOLUME` with:
#   "dockerfile invalid: docker VOLUME at Line X is not supported, use Railway Volumes"
#
# To attach persistent storage for the SQLite database (/app/data/rvg_gateway.db):
#
# 1. Via Railway Dashboard:
#    - Open your project on Railway (https://railway.app)
#    - Click "+ New" -> Select "Volume"
#    - Set the Mount Path to: /app/data
#    - Connect the Volume to this service
#
# 2. Via Docker Compose (docker-compose.yml):
#    services:
#      rvg-gateway:
#        build: .
#        ports:
#          - "8000:8000"
#          - "1080:1080"
#        volumes:
#          - rvg_data:/app/data
#    volumes:
#      rvg_data:
#
# 3. Via Docker CLI:
#    docker run -d -p 8000:8000 -p 1080:1080 -v rvg_data:/app/data rvg-gateway
# ==============================================================================
RUN mkdir -p /app/data

# Expose HTTP/WS and SOCKS5 ports
EXPOSE 8000 1080

# Healthcheck for container orchestrators (Railway / Docker Swarm / K8s)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/api/health || exit 1

# Launch FastAPI app with Uvicorn
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
