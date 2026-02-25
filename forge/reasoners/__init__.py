"""FORGE reasoners — registered on the AgentField router."""

from agentfield import AgentRouter

router = AgentRouter(tags=["forge-engine"])

from . import discovery  # noqa: E402, F401
from . import triage  # noqa: E402, F401
from . import remediation  # noqa: E402, F401
from . import validation  # noqa: E402, F401

__all__ = ["router"]
