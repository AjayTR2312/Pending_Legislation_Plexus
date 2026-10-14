"""
Anthropic credentials from the TR AI Platform.

Two ways to get a key, tried in this order (same design as the XML track-changes project):

  1. CIAM M2M — OAuth2 client credentials through ``tr_ai_platform_sdk``:
         CIAM_CLIENT_ID, CIAM_CLIENT_SECRET, ASSET_ID
  2. Workspace token exchange — the older flow, used when CIAM is not configured or fails:
         TR_WORKSPACE_ID, TR_ANTHROPIC_TOKEN_URL

Both read their settings through app/settings.py (process environment > project ``.env`` > default).
A CIAM failure is never fatal on its own — it
falls back to the workspace flow — and no secret is ever logged: error text is scrubbed of
every configured credential value before it reaches a log line.

    from ai_credentials import get_anthropic_api_key, LAST_SOURCE
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

import settings

logger = logging.getLogger("ai_credentials")

# Error text that marks a token request as worth retrying (transient network / gateway trouble).
_RETRYABLE = ("timed out", "timeout", "readtimeout", "connection", "disconnected", "handshake",
              "500", "502", "503", "504", "529")

LAST_SOURCE: Optional[str] = None  # "ciam" or "workspace" — which path produced the last key

_ciam_client = None
_ciam_lock = threading.Lock()
_SECRET_ENV = ("CIAM_CLIENT_SECRET", "CIAM_CLIENT_ID", "ASSET_ID", "ANTHROPIC_API_KEY")


def _scrub(text: str) -> str:
    """Remove any configured credential value from text that is about to be logged."""
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        if len(value) >= 6:
            text = text.replace(value, "***")
    return text


def ciam_configured() -> bool:
    return all(settings.get(k) for k in ("CIAM_CLIENT_ID", "CIAM_CLIENT_SECRET", "ASSET_ID"))


def _get_ciam_client():
    global _ciam_client
    if _ciam_client is not None:
        return _ciam_client
    with _ciam_lock:
        if _ciam_client is None:
            from tr_ai_platform_sdk import ai_platform  # raises ImportError if the SDK is absent

            _ciam_client = ai_platform(
                "llm",
                client_id=settings.CIAM_CLIENT_ID,
                client_secret=settings.CIAM_CLIENT_SECRET,
                asset_id=settings.ASSET_ID,
            )
    return _ciam_client


def _key_from_ciam() -> Optional[str]:
    if not ciam_configured():
        return None
    previous = os.environ.get("ANTHROPIC_API_KEY")
    try:
        os.environ.pop("ANTHROPIC_API_KEY", None)  # so a stale value can never pass for a fresh one
        _get_ciam_client().auth("anthropic")  # the SDK exports ANTHROPIC_API_KEY as a side effect
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if key:
            return key
        logger.warning("CIAM auth returned no Anthropic key — falling back to the workspace token flow")
    except Exception as err:  # noqa: BLE001 — never fatal: the workspace flow is the fallback
        logger.warning("CIAM auth failed (%s: %s) — falling back to the workspace token flow",
                       type(err).__name__, _scrub(str(err))[:160])
    if previous is not None:
        os.environ["ANTHROPIC_API_KEY"] = previous
    return None


def _post_token_request(token_url: str, workspace_id: str, verify_tls: bool, timeout: int):
    """POST the workspace token request, retrying transient failures on the configured delays."""
    import requests

    delays = settings.TOKEN_RETRY_DELAYS_S or (0,)
    tries = len(delays)
    for attempt in range(1, tries + 1):
        try:
            resp = requests.post(token_url, headers=None, json={"workspace_id": workspace_id},
                                 timeout=timeout, verify=verify_tls)
            resp.raise_for_status()
            return resp
        except Exception as err:  # noqa: BLE001 — classified by message, as the gateway reports it
            retryable = any(k in str(err).lower() for k in _RETRYABLE)
            if retryable and attempt < tries:
                logger.warning("Token request failed (attempt %d/%d): %s — retrying in %ss",
                               attempt, tries, _scrub(str(err))[:160], delays[attempt - 1])
                time.sleep(delays[attempt - 1])
                continue
            raise
    raise RuntimeError("token request failed")  # pragma: no cover


def _key_from_workspace(verify_tls: bool, timeout: int) -> str:
    resp = _post_token_request(settings.TR_ANTHROPIC_TOKEN_URL, settings.TR_WORKSPACE_ID, verify_tls, timeout)
    credentials = json.loads(resp.content)
    key = credentials.get("anthropic_api_key") if isinstance(credentials, dict) else None
    if not key:
        # Report what the gateway said without echoing any value it returned.
        got = sorted(credentials) if isinstance(credentials, dict) else type(credentials).__name__
        raise RuntimeError(f"No anthropic_api_key in the gateway response (it returned: {got})")
    return key


def get_anthropic_api_key(verify_tls: bool = True, timeout: Optional[int] = None) -> str:
    """CIAM M2M first, workspace token second. Raises RuntimeError if neither yields a key."""
    global LAST_SOURCE
    timeout = timeout or settings.TOKEN_TIMEOUT_S
    key = _key_from_ciam()
    if key:
        LAST_SOURCE = "ciam"
        logger.info("Anthropic credentials obtained via CIAM M2M auth")
        return key
    key = _key_from_workspace(verify_tls, timeout)
    LAST_SOURCE = "workspace"
    logger.info("Anthropic credentials obtained via the workspace token flow")
    return key
