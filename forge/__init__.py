"""FORGE — Framework for Orchestrated Remediation & Governance Engine.

A multi-agent AI system that takes vibe-coded MVPs and systematically
hardens them for production deployment.
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("vibe2prod")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
