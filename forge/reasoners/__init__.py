"""FORGE reasoners — discovery and triage agents.

In standalone mode (CLI), the reasoner functions are imported directly
by ``forge.standalone.StandaloneDispatcher``.
"""


class _StubRouter:
    def reasoner(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


router = _StubRouter()

from . import discovery  # noqa: E402, F401
from . import triage  # noqa: E402, F401

__all__ = ["router"]
