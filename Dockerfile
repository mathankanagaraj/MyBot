###############################
# Optimized Dockerfile for Angel One and IBKR Trading Bot
# Base: Debian Slim (Best balance of size vs compatibility)
# Optimization: Multi-stage build + cache cleanup
###############################

###############################
# Stage 1 — Builder
###############################
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /build

# Install build dependencies mostly for potential source builds
# (Even usually not needed for manylinux wheels, but good safety)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install dependencies into /install
# --no-cache-dir: Don't save pip cache
# --prefix=/install: Install to specific directory
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

# Cleanup python bytecode to save space
RUN find /install -type d -name __pycache__ -exec rm -rf {} + \
    && find /install -type f -name "*.pyc" -delete \
    && find /install -type f -name "*.pyo" -delete

###############################
# Stage 2 — Runtime
###############################
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kolkata \
    PATH="/usr/local/bin:$PATH"

WORKDIR /app

# Install minimal runtime system dependencies
# tzdata: for timezone support
# curl: optional, useful for healthchecks (can remove if not needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -ms /bin/bash botuser

# Copy installed python packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=botuser:botuser src/ /app/

# Create necessary directories
RUN mkdir -p /app/logs /app/audit && \
    chown -R botuser:botuser /app

USER botuser

CMD ["python", "-u", "main.py"]
