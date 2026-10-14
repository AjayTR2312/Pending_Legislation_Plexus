"""
One place for the LLM model configuration, pricing and automatic fallback.

    PRIMARY_MODEL   the model every call asks for          (default claude-opus-5-5)
    FALLBACK_MODEL  used when the primary cannot serve it  (default claude-opus-4-7)

Override without touching code, in the project .env or the environment:

    LLM_MODEL=claude-opus-5-5
    LLM_FALLBACK_MODEL=claude-opus-4-7        # `none` = never fall back

Every value here comes from app/settings.py (see `python app/settings.py --show`); the price
sheet is config/model_pricing.json (LLM_PRICING_FILE).

`wrap_client()` returns a client whose `messages.create()` and `messages.stream()`
behave like the Anthropic SDK's but:

  * retry the SAME model a couple of times on a transient "overloaded" (529),
  * then switch to the fallback model if the primary is unavailable (404 / model
    not found / no access), still overloaded, or answering 5xx,
  * never fall back for errors that would fail identically on any model
    (bad API key, malformed request, content problems) — those are raised as-is.

Which model actually answered is recorded (see `usage_summary()`), so cost reports use
the right price and a job log can say "fell back to claude-opus-4-8".

Prompt caching: a large `system` prompt is sent with a cache marker, so repeat calls (the next chunk,
the next document of the same state) read it at the cached rate. If the gateway rejects the marker the
call is retried without it and caching switches itself off for the process — it can never fail a call.

Cost ledger: every call is recorded with its phase — `phase("conversion")` (the default) or
`phase("validation")` — so conversion cost can be reported first and validation cost added only when
someone actually ran an LLM validation. See `ledger_totals()`.

Check availability and price of the configured models (makes 2 tiny API calls):

    python app/llm_models.py --check
    python app/llm_models.py --check --check-cache   # also proves prompt caching works (two more calls)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Optional

import settings

logger = logging.getLogger("llm_models")

PRIMARY_MODEL: str = settings.LLM_MODEL
FALLBACK_MODEL: str = "" if settings.LLM_FALLBACK_MODEL.lower() in ("none", "off", "disabled") else settings.LLM_FALLBACK_MODEL

# Last-resort copy of the price sheet, used only if config/model_pricing.json cannot be read.
_BUILTIN_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.2},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.5},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.5},
}


def load_pricing() -> dict[str, dict[str, float]]:
    """USD per 1M tokens, from the price sheet in LLM_PRICING_FILE (config/model_pricing.json)."""
    path = settings.LLM_PRICING_FILE
    try:
        models = json.loads(path.read_text(encoding="utf-8"))["models"]
        for model, p in models.items():
            for key in ("input", "output", "cache_write", "cache_read"):
                p[key] = float(p[key])
        return models
    except (OSError, ValueError, KeyError, TypeError) as err:
        logger.warning("Could not read the price sheet %s (%s) — using the built-in copy", path, err)
        return dict(_BUILTIN_PRICING)


MODEL_PRICING: dict[str, dict[str, float]] = load_pricing()

_warned_models: set[str] = set()


def pricing_for(model: Optional[str]) -> dict[str, float]:
    """Price sheet for a model. An unknown model is billed at the most expensive known tier,
    never silently at a cheaper one."""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    if model and model not in _warned_models:
        _warned_models.add(model)
        logger.warning("No price listed for model %r — costing it at the highest listed rate.", model)
    return max(MODEL_PRICING.values(), key=lambda p: p["output"])


def calculate_cost(input_tokens: int, output_tokens: int, model: Optional[str] = None,
                   cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    p = pricing_for(model or PRIMARY_MODEL)
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_write_tokens * p["cache_write"]
        + cache_read_tokens * p["cache_read"]
    ) / 1_000_000


# ── Which errors justify switching model ─────────────────────────────────────

def _status(err: BaseException) -> Optional[int]:
    code = getattr(err, "status_code", None)
    if code is None:
        code = getattr(getattr(err, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def is_overloaded(err: BaseException) -> bool:
    msg = str(err).lower()
    return _status(err) == 529 or "overloaded" in msg or "529" in msg


def model_unavailable(err: BaseException) -> bool:
    """The model itself cannot be used (unknown to the gateway, or this key has no access)."""
    status, msg = _status(err), str(err).lower()
    if status == 404:
        return True
    if status in (400, 403) and "model" in msg:
        return True
    return any(k in msg for k in (
        "model not found", "unknown model", "invalid model", "model_not_found",
        "not supported model", "model is not supported", "does not exist", "no access to model",
    ))


def is_server_error(err: BaseException) -> bool:
    return _status(err) in (500, 502, 503, 504)


def _switch_worthy(err: BaseException) -> bool:
    return model_unavailable(err) or is_overloaded(err) or is_server_error(err)


# ── Which model answered ─────────────────────────────────────────────────────

_lock = threading.Lock()
_used: Counter = Counter()
_fallbacks: list[dict[str, str]] = []
_local = threading.local()


def _record(requested: str, used: str, reason: str = "") -> None:
    with _lock:
        _used[used] += 1
        if used != requested:
            _fallbacks.append({"requested": requested, "used": used, "reason": reason[:160]})
    _local.last = used


def last_model_used() -> Optional[str]:
    """The model that answered the most recent call made on this thread."""
    return getattr(_local, "last", None)


def reset_usage() -> None:
    with _lock:
        _used.clear()
        _fallbacks.clear()


def usage_summary() -> dict[str, Any]:
    """Models used since the last reset_usage(), and any fallbacks, for logs and job reports."""
    with _lock:
        models = dict(_used)
        events = list(_fallbacks)
    return {
        "models": models,
        "primary": PRIMARY_MODEL,
        "fallback_used": bool(events),
        "fallback_events": events,
    }


# ── Prompt caching ───────────────────────────────────────────────────────────

CACHE_ENABLED: bool = settings.LLM_PROMPT_CACHE
CACHE_MIN_CHARS: int = settings.LLM_CACHE_MIN_CHARS
_CACHE_MARK = {"type": "ephemeral"}  # the default 5-minute lifetime, which is what the price sheet's cache_write assumes
_cache_blocked = False               # set when the gateway rejects cache markers


CACHE_AUTO: bool = settings.LLM_CACHE_AUTO
CACHE_WARM_S: int = settings.LLM_CACHE_WARM_S
_hint = False                         # the caller knows more work for the same prompt is coming
_warm: dict[str, float] = {}          # prompt fingerprint -> when we last wrote/read its cache entry


@contextlib.contextmanager
def cache_hint(expect_more: bool) -> Iterator[None]:
    """Tell the cache logic that more calls with the same prompt are about to follow (e.g. same-state jobs queued)."""
    global _hint
    previous, _hint = _hint, bool(expect_more)
    try:
        yield
    finally:
        _hint = previous


def cache_decision(key: Optional[str] = None) -> tuple[bool, str]:
    """(cache this request?, why). In auto mode a cache write is only worth its 25% premium when something will read it."""
    if not CACHE_ENABLED:
        return False, "caching is turned off (LLM_PROMPT_CACHE=false)"
    if _cache_blocked:
        return False, "the gateway rejected cache markers"
    if not CACHE_AUTO:
        return True, "always on (LLM_CACHE_AUTO=false)"
    if _hint:
        return True, "more jobs for this state are queued"
    if key is not None and time.monotonic() - _warm.get(key, float("-inf")) < CACHE_WARM_S:
        return True, "the cache is already warm"
    return False, "a lone document would only pay the write premium"


def cache_wanted(key: Optional[str] = None) -> bool:
    return cache_decision(key)[0]


def apply_cache(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Mark a large string `system` prompt cacheable. Anything else is left exactly as given."""
    system = kwargs.get("system")
    if isinstance(system, str) and len(system) >= CACHE_MIN_CHARS:
        key = hashlib.sha1(system.encode("utf-8", "replace")).hexdigest()
        if cache_wanted(key):
            _warm[key] = time.monotonic()
            return {**kwargs, "system": [{"type": "text", "text": system, "cache_control": dict(_CACHE_MARK)}]}
    return kwargs


def cache_breakpoint(block: dict[str, Any]) -> dict[str, Any]:
    """For callers that build content blocks: mark one block as the end of a cacheable prefix."""
    return {**block, "cache_control": dict(_CACHE_MARK)} if cache_wanted() else block


def has_cache_control(kwargs: dict[str, Any]) -> bool:
    system = kwargs.get("system")
    if isinstance(system, list) and any(isinstance(b, dict) and "cache_control" in b for b in system):
        return True
    for msg in kwargs.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and any(isinstance(b, dict) and "cache_control" in b for b in content):
            return True
    return False


def strip_cache(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The same request with every cache marker removed (a list system prompt goes back to a string)."""
    out = dict(kwargs)
    system = out.get("system")
    if isinstance(system, list):
        if all(isinstance(b, dict) and b.get("type") == "text" for b in system):
            out["system"] = "".join(b.get("text", "") for b in system)
        else:
            out["system"] = [{k: v for k, v in b.items() if k != "cache_control"} for b in system]
    if out.get("messages"):
        out["messages"] = [
            {**m, "content": [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b
                              for b in m["content"]]}
            if isinstance(m, dict) and isinstance(m.get("content"), list) else m
            for m in out["messages"]
        ]
    return out


def cache_rejected(err: BaseException) -> bool:
    """The gateway/model refused the request because of the cache marker itself."""
    return _status(err) in (400, 422) and "cache" in str(err).lower()


def _disable_cache(err: BaseException) -> None:
    global _cache_blocked
    if not _cache_blocked:
        _cache_blocked = True
        logger.warning("Prompt caching rejected (%s) — retrying without it and switching it off for this process.",
                       str(err)[:140])


# ── Cost ledger: conversion first, validation only if someone runs it ────────

DEFAULT_PHASE = "conversion"


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """Attribute every API call made inside the block to `name` (e.g. "validation")."""
    previous = getattr(_local, "phase", DEFAULT_PHASE)
    _local.phase = name
    try:
        yield
    finally:
        _local.phase = previous


def ledger_reset() -> None:
    """Forget this thread's recorded calls (start of a conversion or an audit)."""
    _local.entries = []


def response_text(response: Any) -> str:
    """Text of a reply, skipping thinking/tool blocks (content[0] is not always text on newer models)."""
    return "".join(getattr(b, "text", "") or "" for b in getattr(response, "content", []) if getattr(b, "type", "") == "text")


def model_label(model: Optional[str] = None) -> str:
    """'claude-opus-5-5' -> 'Claude Opus 5.5' (for logs and UI text)."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?", model or PRIMARY_MODEL)
    if not m:
        return model or PRIMARY_MODEL
    return f"Claude {m.group(1).title()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")


def _ledger_add(model: str, usage: Any) -> None:
    if usage is None:
        return
    num = lambda attr: int(getattr(usage, attr, 0) or 0)  # noqa: E731
    entry = {
        "phase": getattr(_local, "phase", DEFAULT_PHASE), "model": model,
        "input_tokens": num("input_tokens"), "output_tokens": num("output_tokens"),
        "cache_write_tokens": num("cache_creation_input_tokens"), "cache_read_tokens": num("cache_read_input_tokens"),
    }
    entry["cost_usd"] = calculate_cost(entry["input_tokens"], entry["output_tokens"], model,
                                       entry["cache_write_tokens"], entry["cache_read_tokens"])
    p = pricing_for(model)
    # what caching saved (or cost) versus sending those same tokens at the plain input price
    entry["cache_saving_usd"] = (entry["cache_read_tokens"] * (p["input"] - p["cache_read"])
                                 - entry["cache_write_tokens"] * (p["cache_write"] - p["input"])) / 1_000_000
    logger.info("💰 %s [%s] %s in / %s out · cache write %s / read %s · $%.4f", model, entry["phase"],
                f"{entry['input_tokens']:,}", f"{entry['output_tokens']:,}", f"{entry['cache_write_tokens']:,}",
                f"{entry['cache_read_tokens']:,}", entry["cost_usd"])
    if not hasattr(_local, "entries"):
        _local.entries = []
    _local.entries.append(entry)


def ledger_totals(phase_name: Optional[str] = None) -> dict[str, Any]:
    """Totals for one phase (or every phase when None) since the last ledger_reset()."""
    rows = [e for e in getattr(_local, "entries", []) if phase_name is None or e["phase"] == phase_name]
    models: Counter = Counter(e["model"] for e in rows)
    total = lambda key: sum(e[key] for e in rows)  # noqa: E731
    return {
        "api_calls": len(rows),
        "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
        "cache_write_tokens": total("cache_write_tokens"), "cache_read_tokens": total("cache_read_tokens"),
        "cost_usd": round(total("cost_usd"), 6), "cache_saving_usd": round(total("cache_saving_usd"), 6),
        "model": ", ".join(models), "models": dict(models),
    }


# ── The wrapper ──────────────────────────────────────────────────────────────

PRIMARY_ATTEMPTS = settings.LLM_PRIMARY_ATTEMPTS  # same-model retries on "overloaded" before switching
LAST_ATTEMPTS = settings.LLM_LAST_ATTEMPTS        # the last model has nowhere to go, so try harder


def _chain(model: Optional[str]) -> list[str]:
    """Models to try, in order, for a call that asked for `model`."""
    model = model or PRIMARY_MODEL
    if FALLBACK_MODEL and FALLBACK_MODEL != model:
        return [model, FALLBACK_MODEL]
    return [model]


def _backoff(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 30))


class _FallbackStream:
    """Context manager: opens the stream on the first model that will serve it."""

    def __init__(self, messages: Any, kwargs: dict[str, Any]):
        self._messages, self._kwargs = messages, apply_cache(kwargs)
        self._cm: Any = None
        self._stream: Any = None
        self._model: str = kwargs.get("model") or PRIMARY_MODEL

    def __enter__(self):
        requested = self._kwargs.get("model") or PRIMARY_MODEL
        chain = _chain(requested)
        for i, model in enumerate(chain):
            last_model = i == len(chain) - 1
            attempts = LAST_ATTEMPTS if last_model else PRIMARY_ATTEMPTS
            for attempt in range(1, attempts + 1):
                cm = self._messages.stream(**{**self._kwargs, "model": model})
                try:
                    stream = cm.__enter__()
                except Exception as err:  # noqa: BLE001 — classified below
                    if has_cache_control(self._kwargs) and cache_rejected(err):
                        _disable_cache(err)
                        self._kwargs = strip_cache(self._kwargs)
                        continue
                    if is_overloaded(err) and attempt < attempts:
                        logger.warning("⏳ %s overloaded (attempt %d/%d) — retrying", model, attempt, attempts)
                        _backoff(attempt)
                        continue
                    if not last_model and _switch_worthy(err):
                        logger.warning("↪ %s unavailable for streaming (%s) — falling back to %s",
                                       model, str(err)[:120], chain[i + 1])
                        break
                    raise
                self._cm, self._stream, self._model = cm, stream, model
                _record(requested, model, "" if model == requested else "stream open failed on primary")
                return stream
        raise RuntimeError("No model could open the stream")  # pragma: no cover — loop always returns/raises

    def __exit__(self, *exc):
        if self._stream is not None and exc[0] is None:
            try:  # the caller has normally consumed the stream, so this returns the finished message at once
                _ledger_add(self._model, getattr(self._stream.get_final_message(), "usage", None))
            except Exception:  # noqa: BLE001 — cost bookkeeping must never fail a conversion
                logger.debug("could not read final usage from the stream", exc_info=True)
        return self._cm.__exit__(*exc) if self._cm is not None else False


class _Messages:
    def __init__(self, messages: Any):
        self._messages = messages

    def create(self, **kwargs: Any):
        requested = kwargs.get("model") or PRIMARY_MODEL
        kwargs = apply_cache(kwargs)
        chain = _chain(requested)
        for i, model in enumerate(chain):
            last_model = i == len(chain) - 1
            attempts = LAST_ATTEMPTS if last_model else PRIMARY_ATTEMPTS
            for attempt in range(1, attempts + 1):
                try:
                    response = self._messages.create(**{**kwargs, "model": model})
                except Exception as err:  # noqa: BLE001 — classified below
                    if has_cache_control(kwargs) and cache_rejected(err):
                        _disable_cache(err)
                        kwargs = strip_cache(kwargs)
                        continue
                    if is_overloaded(err) and attempt < attempts:
                        logger.warning("⏳ %s overloaded (attempt %d/%d) — retrying", model, attempt, attempts)
                        _backoff(attempt)
                        continue
                    if not last_model and _switch_worthy(err):
                        logger.warning("↪ %s could not serve the request (%s) — falling back to %s",
                                       model, str(err)[:120], chain[i + 1])
                        _local.pending_reason = str(err)
                        break
                    raise
                _record(requested, model, getattr(_local, "pending_reason", "") if model != requested else "")
                _local.pending_reason = ""
                _ledger_add(model, getattr(response, "usage", None))
                return response
        raise RuntimeError("No model could serve the request")  # pragma: no cover

    def stream(self, **kwargs: Any):
        return _FallbackStream(self._messages, kwargs)

    def __getattr__(self, name: str):  # count_tokens, batches, ...
        return getattr(self._messages, name)


class FallbackClient:
    """Drop-in for anthropic.Anthropic with model fallback on messages.create / messages.stream."""

    def __init__(self, client: Any):
        self._client = client
        self.messages = _Messages(client.messages)

    @property
    def raw(self) -> Any:
        return self._client

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def wrap_client(client: Any) -> Any:
    """Idempotent: wrapping an already-wrapped client returns it unchanged."""
    if client is None or isinstance(client, FallbackClient):
        return client
    return FallbackClient(client)


# ── Availability / price check (run by a person; makes real API calls) ───────

def _check() -> int:  # pragma: no cover — talks to the real API
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pymupdf_extractor import _get_anthropic_client

    import ai_credentials

    base = _get_anthropic_client()
    raw = base.raw if isinstance(base, FallbackClient) else base
    print(f"Auth    : {ai_credentials.LAST_SOURCE} "
          f"({'CIAM M2M' if ai_credentials.LAST_SOURCE == 'ciam' else 'TR_WORKSPACE_ID token fallback'})")
    print(f"Primary : {PRIMARY_MODEL}\nFallback: {FALLBACK_MODEL or '(none)'}\n")
    ok = True
    for model in [m for m in (PRIMARY_MODEL, FALLBACK_MODEL) if m]:
        started = time.monotonic()
        try:
            r = raw.messages.create(model=model, max_tokens=8,
                                    messages=[{"role": "user", "content": "Reply with the single word OK."}])
            u = getattr(r, "usage", None)
            cost = calculate_cost(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0), model)
            print(f"  OK      {model:28} {time.monotonic() - started:5.1f}s  ${cost:.6f}  "
                  f"in/out {getattr(u, 'input_tokens', '?')}/{getattr(u, 'output_tokens', '?')}")
        except Exception as err:  # noqa: BLE001
            ok = False
            kind = "UNAVAILABLE" if model_unavailable(err) else "ERROR"
            print(f"  {kind:11} {model:28} {str(err)[:140]}")
    if ok and "--check-cache" in sys.argv:
        ok = _check_cache(raw) == 0
    return 0 if ok else 1


def _check_cache(raw: Any) -> int:  # pragma: no cover — talks to the real API
    """Send the same long system prompt twice. The 2nd call should READ it from the cache."""
    prompt = ("You are a careful legislative text checker. " + "Preserve every word exactly. " * 900)
    print(f"\nCache probe on {PRIMARY_MODEL}: {len(prompt):,}-char system prompt, two identical calls")
    got_read = False
    for n in (1, 2):
        r = raw.messages.create(
            model=PRIMARY_MODEL, max_tokens=8,
            system=[{"type": "text", "text": prompt, "cache_control": dict(_CACHE_MARK)}],
            messages=[{"role": "user", "content": "Reply with the single word OK."}])
        u = getattr(r, "usage", None)
        write, read = getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0
        got_read = got_read or read > 0
        cost = calculate_cost(getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                              PRIMARY_MODEL, write, read)
        print(f"  call {n}: cache write {write:>6,} | cache read {read:>6,} | uncached in {getattr(u, 'input_tokens', '?')} | ${cost:.5f}")
    print("  RESULT:", "caching works — the 2nd call read the prompt from cache" if got_read else
          "NO cache read seen — this gateway/model is not caching (set LLM_PROMPT_CACHE=false to stop paying the write premium)")
    return 0 if got_read else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_check() if "--check" in sys.argv else print(__doc__) or 0)
