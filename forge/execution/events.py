"""Webhook event emission for FORGE scan progress.

Pushes scan lifecycle events to an external webhook endpoint (e.g. the
vibe2prod backend) so it can stream progress to clients via SSE instead
of polling the sandbox for logs.

All functions are best-effort: if the webhook is unreachable or the
request fails, the scan continues unaffected.  Only stdlib is used
(no ``requests`` or ``httpx`` dependency).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge.config import ForgeConfig

logger = logging.getLogger(__name__)


# ── Core emitter ─────────────────────────────────────────────────────


def emit_event(
    cfg: ForgeConfig,
    event_type: str,
    agent: str,
    message: str,
    level: str = "info",
    data: dict | None = None,
) -> None:
    """POST a signed JSON event to the configured webhook endpoint.

    No-op when ``cfg.webhook_url`` is empty.  Catches *all* exceptions
    so a webhook failure can never crash or slow down the scan.
    """
    if not cfg.webhook_url:
        return

    try:
        payload = {
            "event_type": event_type,
            "agent": agent,
            "message": message,
            "level": level,
            "data": data or {},
            "scan_id": cfg.webhook_scan_id,
        }

        body = json.dumps(payload, sort_keys=True).encode()

        signature = hmac.new(
            cfg.webhook_token.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        req = urllib.request.Request(
            cfg.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Forge-Signature": f"sha256={signature}",
            },
            method="POST",
        )

        urllib.request.urlopen(req, timeout=5)  # noqa: S310

    except Exception:
        logger.warning(
            "Webhook emit failed (non-fatal): event_type=%s agent=%s",
            event_type,
            agent,
            exc_info=True,
        )


# ── Convenience wrappers ─────────────────────────────────────────────


def emit_phase_start(cfg: ForgeConfig, phase: str, message: str) -> None:
    """Emit an ``agent_start`` event for a pipeline phase."""
    emit_event(cfg, event_type="agent_start", agent=phase, message=message)


def emit_phase_complete(cfg: ForgeConfig, phase: str, message: str) -> None:
    """Emit an ``agent_complete`` event for a pipeline phase."""
    emit_event(cfg, event_type="agent_complete", agent=phase, message=message)


def emit_scan_complete(
    cfg: ForgeConfig, message: str, data: dict | None = None
) -> None:
    """Emit a ``scan_complete`` event at the end of a successful run."""
    emit_event(
        cfg, event_type="scan_complete", agent="orchestrator",
        message=message, data=data,
    )


def emit_scan_error(cfg: ForgeConfig, message: str) -> None:
    """Emit a ``scan_error`` event when the scan fails."""
    emit_event(
        cfg, event_type="scan_error", agent="orchestrator",
        message=message, level="error",
    )
