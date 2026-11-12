# Image for every service of the local demo (docker compose -f demo/compose.yaml).
# Separate from the root lab Dockerfile and Dockerfile.telemetry, which stay unchanged.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/services:/app
COPY pyproject.toml requirements-runtime.lock ./
COPY ml/team_starter/requirements-ml-inference.lock ./ml-inference.lock
RUN pip install --no-cache-dir -r requirements-runtime.lock -r ml-inference.lock
COPY services ./services
COPY ml ./ml
COPY scripts/end_to_end.py ./scripts/end_to_end.py
COPY demo ./demo
RUN useradd --uid 10001 --create-home demo && mkdir -p /app/data && chown demo:demo /app/data
USER demo
CMD ["python", "-m", "demo.lab.controller"]
