FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        dumb-init \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better Docker layer caching
COPY pyproject.toml README.md ./
COPY trading_bot ./trading_bot

RUN pip install --upgrade pip \
    && pip install --no-cache-dir ".[dev]" \
    && pip install --no-cache-dir --no-deps .

# Create runtime data directory.
# Railway volume mounted at /app/data may be owned by root at runtime,
# so permissions must allow the non-root botuser to write logs/manifests.
RUN mkdir -p /app/data \
    && chmod -R 777 /app/data

# Create non-root user and ensure app is writable where needed
RUN useradd -m botuser \
    && chown -R botuser:botuser /app \
    && chmod -R 777 /app/data

USER botuser

EXPOSE 8080

ENTRYPOINT ["dumb-init", "--"]

CMD ["trading-bot-api", "--host", "0.0.0.0"]
