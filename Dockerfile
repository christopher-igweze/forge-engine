FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (git needed for repo cloning)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY forge/ forge/

# Install Opengrep first so its CLI is on PATH when FORGE runs the
# deterministic SAST phase. Without this, deterministic scans silently
# no-op and reports come back empty.
RUN pip install --no-cache-dir opengrep

# Install forge-engine (no platform extras needed)
RUN pip install --no-cache-dir ".[dev]"

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

# Create workspaces directory for repo cloning
RUN mkdir -p /workspaces && chown appuser:appuser /workspaces

USER appuser

# Default: run as CLI tool (override with docker run args)
ENTRYPOINT ["vibe2prod"]
CMD ["scan", "--help"]
