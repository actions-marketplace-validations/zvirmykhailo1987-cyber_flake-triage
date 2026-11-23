FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    FLAKE_HOME=/app FLAKE_REPLAY_ONLY=1 PORT=8070 FORWARDED_ALLOW_IPS=127.0.0.1
WORKDIR /app
COPY requirements.lock pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir -c requirements.lock . && useradd -u 10001 -m triage
# the committed data, the model's recorded answers and the built report: the console replays, never calls
COPY data/openaev/runs.jsonl.gz data/openaev/meta.json ./data/openaev/
COPY data/openaev/excerpts-e7.jsonl.gz ./data/openaev/
COPY evals ./evals
COPY docs/report.html docs/simulation.json ./docs/
USER 10001
EXPOSE 8070
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8070/health').status==200 else 1)"
CMD ["sh", "-c", "uvicorn 'flaketriage.web:create_app' --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
