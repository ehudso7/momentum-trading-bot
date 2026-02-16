FROM python:3.11-slim

WORKDIR /app

# Install dumb-init for proper signal forwarding (SIGTERM to bot process)
# and curl for health checks
RUN apt-get update && \
    apt-get install -y --no-install-recommends dumb-init curl && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies first (cache layer)
# Python 3.11+ has tomllib built-in — extract deps and install separately
COPY pyproject.toml ./
RUN python -c "import tomllib; deps=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; print('\n'.join(deps))" > /tmp/requirements.txt && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# Copy source and install the package (deps already cached)
COPY trading_bot/ trading_bot/
RUN pip install --no-cache-dir --no-deps .

# Create data directory for journal
RUN mkdir -p data

# Non-root user for safety
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

EXPOSE 8080

# Health check: verify the dashboard is responsive
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["curl", "-f", "http://localhost:8080/healthz"]

# Use dumb-init as PID 1 for proper signal forwarding
# This ensures SIGTERM from Docker/Railway reaches the Python process
ENTRYPOINT ["dumb-init", "--"]
CMD ["trading-bot", "--mode", "paper"]
