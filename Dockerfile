###############################
# Stage 1 — Build dependencies
###############################
# Explicitly target ARM64 for OCI free tier
FROM --platform=linux/arm64 python:3.12-slim-bullseye AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    libffi-dev \
    libssl-dev \
    libbz2-dev \
    liblzma-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


###############################
# Stage 2 — Runtime image
###############################
FROM --platform=linux/arm64 python:3.12-slim-bullseye

ENV TZ=Asia/Kolkata
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN useradd -ms /bin/bash botuser

WORKDIR /app

# Copy Python dependencies from builder
COPY --from=builder /install /usr/local

# Copy app source
COPY --chown=botuser:botuser src/ /app/

# Create log/audit folders
RUN mkdir -p /app/logs /app/audit && \
    chown -R botuser:botuser /app/logs /app/audit

USER botuser

CMD ["python", "-u", "main.py"]
