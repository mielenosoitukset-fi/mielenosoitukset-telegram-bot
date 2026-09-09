FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY bot ./bot

RUN pip install --no-cache-dir -e .

CMD ["mielenosoitukset-bot"]
