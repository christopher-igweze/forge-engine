"""Entry point for ``python -m forge``.

Starts the FORGE AgentField node, registering it with the control plane.
"""

import os

from forge.app import app


def main() -> None:
    port = int(os.getenv("FORGE_PORT", "8004"))
    host = os.getenv("FORGE_HOST", "0.0.0.0")
    app.run(port=port, host=host)


if __name__ == "__main__":
    main()
