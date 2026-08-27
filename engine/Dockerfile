# Melbourne Agent Village — Simulation Engine (ECS Fargate, long-running)
# linux/arm64 (Graviton) — builds natively on the ARM64 build host; pure-Python engine.
FROM --platform=linux/arm64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Australia/Melbourne

WORKDIR /app

# tzdata for zoneinfo (Australia/Melbourne DST-correct offsets).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir boto3

COPY village/ ./village/
COPY main.py ./main.py

# Non-root runtime user.
RUN useradd -m -u 10001 village && chown -R village:village /app
USER village

ENTRYPOINT ["python", "-u", "main.py"]
