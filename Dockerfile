# ==========================================
# RVG Gateway Dockerfile
# Multi-stage build with official Xray-core & FastAPI Management Panel
# ==========================================

# Stage 1: Download official Xray-core binary (XTLS/Xray-core)
FROM alpine:3.19 AS xray-builder
RUN apk add --no-cache curl unzip
WORKDIR /tmp
RUN curl -L -H "Cache-Control: no-cache" -o xray.zip \
    https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip && \
    unzip xray.zip && \
    chmod +x xray

# Stage 2: Production Python Runtime
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    FASTAPI_INTERNAL_PORT=8000 \
    XRAY_API_PORT=10085 \
    XRAY_CONFIG_PATH=/app/config.json

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Copy Xray-core binary and geodata files from builder
COPY --from=xray-builder /tmp/xray /usr/local/bin/xray
COPY --from=xray-builder /tmp/geosite.dat /usr/local/share/xray/geosite.dat
COPY --from=xray-builder /tmp/geoip.dat /usr/local/share/xray/geoip.dat

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure storage directories and execution permissions
RUN mkdir -p /app/data /tmp && \
    chmod +x /app/start.sh /usr/local/bin/xray || true

# Expose ports
EXPOSE 8080 8000 1080

# Healthcheck for container orchestrators (Railway / Docker Swarm)
HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/api/health || exit 1

# Launch application via supervisor start script
CMD ["/app/start.sh"]
