FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY finn ./finn
COPY web ./web
COPY data/portfolio.json data/network.json data/scenarios.json ./data/
COPY scripts ./scripts

# Non-root at runtime.
RUN useradd --create-home finn && chown -R finn /app
USER finn

ENV PORT=8000 WEB_CONCURRENCY=2
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health', timeout=4)"
# Several worker processes (the load test showed one process is CPU-bound under concurrency);
# exactly one of them becomes the scheduler via a Postgres advisory lock.
CMD ["sh", "-c", "uvicorn finn.api:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers"]
