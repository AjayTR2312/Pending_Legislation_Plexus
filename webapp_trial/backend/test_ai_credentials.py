"""CIAM-first / workspace-fallback credential resolution — fake SDK and fake HTTP, never the network."""

import json
import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
import ai_credentials as ac  # noqa: E402
import settings  # noqa: E402

CLIENT_ID = "ciam-client-id-0123456789"
CLIENT_SECRET = "ciam-client-secret-abcdefghij"
ASSET = "a123456"
CIAM_KEY = "sk-ant-from-ciam-0001"
WORKSPACE_KEY = "sk-ant-from-workspace-0002"


class FakeResponse:
    def __init__(self, payload):
        self.content = json.dumps(payload).encode()

    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """The real .env is loaded at import, so pin every setting and block the network."""
    for k in ("CIAM_CLIENT_ID", "CIAM_CLIENT_SECRET", "ASSET_ID", "ANTHROPIC_API_KEY",
              "TR_WORKSPACE_ID", "TR_ANTHROPIC_TOKEN_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ac, "_ciam_client", None)
    monkeypatch.setattr(ac, "LAST_SOURCE", None)
    calls = []

    def post(url, **kw):
        calls.append({"url": url, **kw})
        return FakeResponse({"anthropic_api_key": WORKSPACE_KEY})

    import requests
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.delitem(sys.modules, "tr_ai_platform_sdk", raising=False)
    return calls


def configure_ciam(monkeypatch):
    monkeypatch.setenv("CIAM_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("CIAM_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("ASSET_ID", ASSET)


def install_sdk(monkeypatch, on_auth):
    seen = {}

    class Client:
        def auth(self, provider):
            seen["provider"] = provider
            on_auth()

    def ai_platform(kind, **kw):
        seen["init"] = (kind, kw)
        return Client()

    monkeypatch.setitem(sys.modules, "tr_ai_platform_sdk", types.SimpleNamespace(ai_platform=ai_platform))
    return seen


def test_ciam_is_used_first_and_the_workspace_flow_is_not_called(monkeypatch, isolated):
    configure_ciam(monkeypatch)
    seen = install_sdk(monkeypatch, lambda: monkeypatch.setenv("ANTHROPIC_API_KEY", CIAM_KEY))
    assert ac.get_anthropic_api_key() == CIAM_KEY
    assert ac.LAST_SOURCE == "ciam" and isolated == []
    assert seen["provider"] == "anthropic"
    assert seen["init"] == ("llm", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "asset_id": ASSET})


def test_falls_back_to_the_workspace_token_when_ciam_is_not_configured(monkeypatch, isolated):
    monkeypatch.setenv("CIAM_CLIENT_ID", CLIENT_ID)  # secret + asset missing -> not configured
    monkeypatch.setenv("TR_WORKSPACE_ID", "MyWorkspace1234")
    monkeypatch.setenv("TR_ANTHROPIC_TOKEN_URL", "https://gateway.example/token")
    assert ac.get_anthropic_api_key(verify_tls=False, timeout=7) == WORKSPACE_KEY
    assert ac.LAST_SOURCE == "workspace"
    call = isolated[0]
    assert call["url"] == "https://gateway.example/token"
    assert call["json"] == {"workspace_id": "MyWorkspace1234"}
    assert call["verify"] is False and call["timeout"] == 7


def test_defaults_apply_when_env_is_silent(isolated):
    ac.get_anthropic_api_key()
    assert isolated[0]["url"] == settings.TR_ANTHROPIC_TOKEN_URL
    assert isolated[0]["json"] == {"workspace_id": settings.TR_WORKSPACE_ID}
    assert isolated[0]["timeout"] == settings.TOKEN_TIMEOUT_S


def test_ciam_failure_falls_back_and_never_logs_a_secret(monkeypatch, isolated, caplog):
    configure_ciam(monkeypatch)

    def boom():
        raise RuntimeError(f"401 for client {CLIENT_ID} secret {CLIENT_SECRET}")

    install_sdk(monkeypatch, boom)
    with caplog.at_level(logging.WARNING, logger="ai_credentials"):
        assert ac.get_anthropic_api_key() == WORKSPACE_KEY
    assert ac.LAST_SOURCE == "workspace"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "falling back" in logged
    assert CLIENT_SECRET not in logged and CLIENT_ID not in logged and "***" in logged


def test_missing_sdk_falls_back(monkeypatch, isolated):
    configure_ciam(monkeypatch)  # configured, but tr_ai_platform_sdk cannot be imported
    monkeypatch.setitem(sys.modules, "tr_ai_platform_sdk", None)  # makes `import` raise ImportError
    assert ac.get_anthropic_api_key() == WORKSPACE_KEY and ac.LAST_SOURCE == "workspace"


def test_a_stale_env_key_is_never_mistaken_for_a_fresh_ciam_key(monkeypatch, isolated):
    configure_ciam(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key-from-an-earlier-run")
    install_sdk(monkeypatch, lambda: None)  # auth "succeeds" but exports nothing
    assert ac.get_anthropic_api_key() == WORKSPACE_KEY
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "stale-key-from-an-earlier-run"  # restored, not clobbered


def test_workspace_response_without_a_key_raises_without_echoing_values(monkeypatch, isolated):
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse({"error": "SECRET-DETAIL-xyz", "code": 7}))
    with pytest.raises(RuntimeError) as exc:
        ac.get_anthropic_api_key()
    assert "code" in str(exc.value) and "SECRET-DETAIL-xyz" not in str(exc.value)


def test_transient_token_errors_are_retried_on_the_configured_delays(monkeypatch):
    import requests

    sleeps, attempts = [], []
    monkeypatch.setattr(ac.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("TOKEN_RETRY_DELAYS_S", "2,4,8")

    def flaky(url, **kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("Connection reset / read timed out")
        return FakeResponse({"anthropic_api_key": WORKSPACE_KEY})

    monkeypatch.setattr(requests, "post", flaky)
    assert ac.get_anthropic_api_key() == WORKSPACE_KEY
    assert len(attempts) == 3 and sleeps == [2, 4]


def test_non_transient_token_errors_are_not_retried(monkeypatch):
    import requests

    attempts = []
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)

    def denied(url, **kw):
        attempts.append(1)
        raise PermissionError("401 Unauthorized: bad workspace")

    monkeypatch.setattr(requests, "post", denied)
    with pytest.raises(PermissionError):
        ac.get_anthropic_api_key()
    assert len(attempts) == 1


def test_a_still_failing_gateway_gives_up_after_the_last_try(monkeypatch):
    import requests

    attempts = []
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)

    def down(url, **kw):
        attempts.append(1)
        raise ConnectionError("503 service unavailable")

    monkeypatch.setattr(requests, "post", down)
    with pytest.raises(ConnectionError):
        ac.get_anthropic_api_key()
    assert len(attempts) == len(settings.TOKEN_RETRY_DELAYS_S)


def test_ciam_configured_needs_all_three(monkeypatch):
    assert not ac.ciam_configured()
    monkeypatch.setenv("CIAM_CLIENT_ID", "x")
    monkeypatch.setenv("CIAM_CLIENT_SECRET", "y")
    assert not ac.ciam_configured()
    monkeypatch.setenv("ASSET_ID", "z")
    assert ac.ciam_configured()


def test_the_project_env_is_wired_up_without_exposing_values():
    """The real .env has the five settings (checked by name only; no value is read into the test output)."""
    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).resolve().parent.parent.parent / ".env")
    for name in ("TR_WORKSPACE_ID", "TR_ANTHROPIC_TOKEN_URL", "CIAM_CLIENT_ID", "CIAM_CLIENT_SECRET", "ASSET_ID"):
        assert (values.get(name) or "").strip(), f"{name} is missing from .env"
