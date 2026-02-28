"""Entry point for ``python -m forge``.

In platform mode (agentfield installed): starts the AgentField node.
In standalone mode: prints a message directing to the CLI.
"""

import json
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal health check handler."""

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logging


def _validate_port(env_var: str, default: int) -> int:
    """Parse and validate a port number from an environment variable."""
    raw = os.getenv(env_var, str(default))
    try:
        port = int(raw)
    except ValueError:
        print(
            f"Warning: {env_var}={raw!r} is not a valid integer. "
            f"Using default port {default}.",
            file=sys.stderr,
        )
        return default
    if not (1 <= port <= 65535):
        print(
            f"Warning: {env_var}={port} is outside valid range (1-65535). "
            f"Using default port {default}.",
            file=sys.stderr,
        )
        return default
    return port


def _start_health_server(port: int) -> None:
    """Start a lightweight health check server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def main() -> None:
    from forge.app import app

    if app is None:
        print(
            "AgentField is not installed. Use the CLI instead:\n\n"
            "  vibe2prod scan ./my-app\n"
            "  vibe2prod fix ./my-app\n\n"
            "To run in platform mode, install agentfield:\n\n"
            "  pip install vibe2prod[platform]\n",
            file=sys.stderr,
        )
        sys.exit(1)

    port = _validate_port("FORGE_PORT", 8004)
    host = os.getenv("FORGE_HOST", "0.0.0.0")
    health_port = _validate_port("FORGE_HEALTH_PORT", 8005)

    _start_health_server(health_port)

    app.run(port=port, host=host)


if __name__ == "__main__":
    main()
