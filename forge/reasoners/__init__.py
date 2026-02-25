"""FORGE reasoners — registered on the AgentField router.

When agentfield is installed (platform mode), reasoners are registered
via AgentRouter decorators.  In standalone mode (CLI), the reasoner
functions are imported directly by ``forge.standalone.StandaloneDispatcher``.
"""

try:
    from agentfield import AgentRouter
    router = AgentRouter(tags=["forge-engine"])
except ImportError:
    # Standalone mode — no AgentField.  Provide a stub router whose
    # .reasoner() decorator is a no-op passthrough.
    class _StubRouter:
        def reasoner(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    router = _StubRouter()

from . import discovery  # noqa: E402, F401
from . import triage  # noqa: E402, F401
from . import remediation  # noqa: E402, F401
from . import validation  # noqa: E402, F401
from . import hive_discovery  # noqa: E402, F401

__all__ = ["router"]
