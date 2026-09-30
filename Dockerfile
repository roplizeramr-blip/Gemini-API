FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    GEMINI_COOKIE_PATH=/data/gemini/cookies \
    GATEWAY_DATA_DIR=/data/gateway \
    SETUPTOOLS_SCM_PRETEND_VERSION=2026.9.30

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY gateway ./gateway
COPY cli.py ./cli.py

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[gateway]" \
    && mkdir -p /data/gemini/cookies /data/gateway/uploads

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn gateway.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
