FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
# Create a minimal stub package so pip can resolve the project's deps
COPY pyproject.toml ./
RUN mkdir -p trading_bot && \
    echo '"""Momentum Trading Bot."""' > trading_bot/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf trading_bot

# Copy actual source
COPY trading_bot/ trading_bot/

# Reinstall with real source (deps already cached, this is fast)
RUN pip install --no-cache-dir --no-deps .

# Create data directory for journal
RUN mkdir -p data

# Non-root user for safety
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

EXPOSE 8080

ENTRYPOINT ["trading-bot"]
CMD ["--mode", "paper"]
