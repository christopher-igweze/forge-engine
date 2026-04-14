FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (git for cloning, curl for downloading Opengrep)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Opengrep binary from GitHub releases. Opengrep ships as a
# standalone Linux binary (no pip package), and FORGE's deterministic
# SAST phase needs it on PATH or the whole scan silently no-ops.
# Pinned to v1.19.0; auto-detects glibc (manylinux) vs musl architecture.
ARG OPENGREP_VERSION=v1.19.0
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64) variant="manylinux_x86" ;; \
        aarch64|arm64) variant="manylinux_aarch64" ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/opengrep/opengrep/releases/download/${OPENGREP_VERSION}/opengrep_${variant}" \
        -o /usr/local/bin/opengrep; \
    chmod +x /usr/local/bin/opengrep; \
    /usr/local/bin/opengrep --version

# Copy project files
COPY pyproject.toml .
COPY forge/ forge/

# Install forge-engine (no platform extras needed)
RUN pip install --no-cache-dir ".[dev]"

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

# Create workspaces directory for repo cloning
RUN mkdir -p /workspaces && chown appuser:appuser /workspaces

USER appuser

# Default: run the MCP server over SSE on port 8004 so hosted deployments
# (e.g. Coolify at forge.verstandai.site) expose a working remote MCP
# endpoint. Local users who install the PyPI package run `forge-mcp` with
# stdio transport directly — this default only affects container deploys.
ENV FORGE_MCP_TRANSPORT=sse \
    FORGE_MCP_HOST=0.0.0.0 \
    FORGE_MCP_PORT=8004
EXPOSE 8004
CMD ["forge-mcp"]
