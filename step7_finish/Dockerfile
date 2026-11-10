FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/services \
    ADAPTIVENET_SHADOW_BIND=0.0.0.0 \
    ADAPTIVENET_SHADOW_PORT=8090
WORKDIR /app
COPY requirements-service.txt /app/requirements-service.txt
RUN pip install --no-cache-dir -r /app/requirements-service.txt \
    && groupadd -g 10001 mlshadow && useradd -M -u 10001 -g mlshadow mlshadow
COPY --chown=10001:10001 . /app
USER 10001:10001
EXPOSE 8090
CMD ["python", "-m", "step7_finish.service"]
