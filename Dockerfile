FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend ./backend
COPY dashboard ./dashboard
COPY config ./config
COPY scripts ./scripts

RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

EXPOSE 8000 8501
# Default: API. The dashboard service overrides the command in docker-compose.yml.
# Single worker on purpose: the cycle lock is per-process.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
