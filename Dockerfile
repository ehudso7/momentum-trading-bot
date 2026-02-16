FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
# Parse dependencies from pyproject.toml and install them separately
COPY pyproject.toml ./
RUN pip install --no-cache-dir tomli 2>/dev/null; \
    python -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    deps = tomllib.load(f)['project']['dependencies']
print('\n'.join(deps))
" > /tmp/requirements.txt && \
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

ENTRYPOINT ["trading-bot"]
CMD ["--mode", "paper"]
