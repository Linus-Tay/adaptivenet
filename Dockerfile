FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements-runtime.lock ./
COPY services ./services
COPY ml ./ml
ENV PYTHONPATH=/app
RUN pip install --no-cache-dir -r requirements-runtime.lock && pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home app && mkdir -p /app/data && chown app:app /app/data
USER app
CMD ["uvicorn", "adaptivenet.control_api:app", "--host", "0.0.0.0", "--port", "8000"]
