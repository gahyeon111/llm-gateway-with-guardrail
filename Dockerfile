FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install ".[redis]" && useradd --uid 10001 --no-create-home gateway

USER 10001
EXPOSE 8080
# One worker per pod; scale with replicas (use CACHE_BACKEND=redis with more than one replica).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
