"""Entry point for ``python -m forge``.

In platform mode (agentfield installed): starts the AgentField node.
In standalone mode: prints a message directing to the CLI.
"""

import os
import sys


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

    port = int(os.getenv("FORGE_PORT", "8004"))
    host = os.getenv("FORGE_HOST", "0.0.0.0")
    app.run(port=port, host=host)


if __name__ == "__main__":
    main()
