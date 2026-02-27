FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (git needed for repo cloning)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project metadata first for layer caching
COPY pyproject.toml .
COPY forge/ forge/

# Install with platform extras (includes agentfield)
RUN pip install --no-cache-dir ".[platform]"

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

# Create workspaces directory for repo cloning
RUN mkdir -p /workspaces && chown appuser:appuser /workspaces

USER appuser

EXPOSE 8004 8005

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8005/health')" || exit 1

CMD ["python", "-m", "forge"]
