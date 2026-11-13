FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Changing this value changes the image digest and forces a new rollout.
ENV RESTART_MARKER=202611131648

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries 10 -r requirements.txt

COPY . .

EXPOSE 8000

RUN python -m compileall -q app alembic
CMD ["sh", "-c", "python -m app.core.ensure_migrated && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
