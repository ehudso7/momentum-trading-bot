FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Copy source
COPY trading_bot/ trading_bot/

# Create data directory for journal
RUN mkdir -p data

# Non-root user for safety
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

VOLUME /app/data

EXPOSE 8080

ENTRYPOINT ["trading-bot"]
CMD ["--mode", "paper"]
