"""Model config, pricing and fallback â€” against fake clients, never the real API."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
import llm_models as lm  # noqa: E402


class ApiError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status


class FakeMessages:
    """create()/stream() answer per model from a script: {model: [outcome, ...]}; an Exception is raised."""

    def __init__(self, script):
        self.script, self.calls = {m: list(v) for m, v in script.items()}, []

    def _next(self, model):
        self.calls.append(model)
        outcome = self.script[model].pop(0) if len(self.script[model]) > 1 else self.script[model][0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def create(self, **kw):
        return SimpleNamespace(model=kw["model"], text=self._next(kw["model"]))

    def stream(self, **kw):
        model = kw["model"]
        fake = self

        class CM:
            def __enter__(self_inner):
                return SimpleNamespace(model=model, text=fake._next(model))

            def __exit__(self_inner, *a):
                return False

        return CM()

    def count_tokens(self, **kw):  # passthrough attribute
        return 42


def client_with(script):
    inner = SimpleNamespace(messages=FakeMessages(script), api_key="k")
    return lm.wrap_client(inner), inner.messages


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(lm, "PRIMARY_MODEL", "claude-opus-5-5")
    monkeypatch.setattr(lm, "FALLBACK_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(lm.time, "sleep", lambda s: None)  # no real backoff in tests
    monkeypatch.setattr(lm, "CACHE_ENABLED", True)
    monkeypatch.setattr(lm, "CACHE_AUTO", False)  # most tests below want "always cache"; auto has its own tests
    monkeypatch.setattr(lm, "_hint", False)
    lm._warm.clear()
    monkeypatch.setattr(lm, "CACHE_MIN_CHARS", 100)
    monkeypatch.setattr(lm, "_cache_blocked", False)
    lm.reset_usage()
    lm.ledger_reset()


# â”€â”€ defaults â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_defaults_are_opus_5_5_then_4_7(monkeypatch):
    import importlib
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
    fresh = importlib.reload(lm)
    assert (fresh.PRIMARY_MODEL, fresh.FALLBACK_MODEL) == ("claude-opus-5-5", "claude-opus-4-7")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "none")
    fresh = importlib.reload(lm)
    assert (fresh.PRIMARY_MODEL, fresh.FALLBACK_MODEL) == ("claude-opus-4-7", "")
    assert fresh._chain("claude-opus-4-7") == ["claude-opus-4-7"]  # `none` = never fall back


# â”€â”€ create(): who answers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_primary_answers_and_no_fallback_is_recorded():
    client, msgs = client_with({"claude-opus-5-5": ["ok"], "claude-opus-4-8": ["should-not-run"]})
    r = client.messages.create(model="claude-opus-5-5", max_tokens=5, messages=[])
    assert r.model == "claude-opus-5-5" and msgs.calls == ["claude-opus-5-5"]
    assert lm.last_model_used() == "claude-opus-5-5"
    assert not lm.usage_summary()["fallback_used"]


@pytest.mark.parametrize("err", [
    ApiError(404, "model: claude-opus-5-5 not found"),
    ApiError(403, "Your key has no access to this model"),
    ApiError(400, "invalid model name"),
    ApiError(529, "Overloaded"),
    ApiError(503, "service unavailable"),
])
def test_falls_back_when_the_primary_cannot_serve(err):
    client, msgs = client_with({"claude-opus-5-5": [err], "claude-opus-4-8": ["ok"]})
    r = client.messages.create(model="claude-opus-5-5", max_tokens=5, messages=[])
    assert r.model == "claude-opus-4-8" and lm.last_model_used() == "claude-opus-4-8"
    s = lm.usage_summary()
    assert s["fallback_used"] and s["fallback_events"][0]["requested"] == "claude-opus-5-5"


def test_one_overload_is_retried_on_the_same_model_not_a_fallback():
    client, msgs = client_with({"claude-opus-5-5": [ApiError(529, "Overloaded"), "ok"], "claude-opus-4-8": ["x"]})
    r = client.messages.create(model="claude-opus-5-5", messages=[])
    assert r.model == "claude-opus-5-5" and msgs.calls == ["claude-opus-5-5", "claude-opus-5-5"]
    assert not lm.usage_summary()["fallback_used"]


def test_persistent_overload_exhausts_retries_then_falls_back():
    client, msgs = client_with({"claude-opus-5-5": [ApiError(529, "Overloaded")], "claude-opus-4-8": ["ok"]})
    client.messages.create(model="claude-opus-5-5", messages=[])
    assert msgs.calls == ["claude-opus-5-5"] * lm.PRIMARY_ATTEMPTS + ["claude-opus-4-8"]


@pytest.mark.parametrize("err", [ApiError(401, "invalid x-api-key"), ApiError(400, "messages: field required"),
                                 ApiError(413, "request too large")])
def test_errors_that_fail_on_any_model_are_not_retried_elsewhere(err):
    client, msgs = client_with({"claude-opus-5-5": [err], "claude-opus-4-8": ["ok"]})
    with pytest.raises(ApiError):
        client.messages.create(model="claude-opus-5-5", messages=[])
    assert msgs.calls == ["claude-opus-5-5"]  # the fallback was never tried


def test_when_the_fallback_fails_too_the_error_is_raised():
    client, _ = client_with({"claude-opus-5-5": [ApiError(404, "model not found")],
                             "claude-opus-4-8": [ApiError(404, "model not found")]})
    with pytest.raises(ApiError):
        client.messages.create(model="claude-opus-5-5", messages=[])


def test_asking_for_the_fallback_model_directly_never_loops():
    client, msgs = client_with({"claude-opus-4-8": [ApiError(529, "Overloaded")]})
    with pytest.raises(ApiError):
        client.messages.create(model="claude-opus-4-8", messages=[])
    assert msgs.calls == ["claude-opus-4-8"] * lm.LAST_ATTEMPTS


# â”€â”€ stream() â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_stream_opens_on_the_fallback_when_the_primary_is_unavailable():
    client, msgs = client_with({"claude-opus-5-5": [ApiError(404, "model not found")], "claude-opus-4-8": ["streamed"]})
    with client.messages.stream(model="claude-opus-5-5", messages=[]) as s:
        assert s.model == "claude-opus-4-8" and s.text == "streamed"
    assert lm.usage_summary()["fallback_used"]


def test_stream_primary_ok_and_unrelated_errors_propagate():
    client, _ = client_with({"claude-opus-5-5": ["fine"], "claude-opus-4-8": ["x"]})
    with client.messages.stream(model="claude-opus-5-5", messages=[]) as s:
        assert s.model == "claude-opus-5-5"
    bad, _ = client_with({"claude-opus-5-5": [ApiError(401, "bad key")], "claude-opus-4-8": ["x"]})
    with pytest.raises(ApiError):
        with bad.messages.stream(model="claude-opus-5-5", messages=[]):
            pass


def test_wrapper_passes_other_attributes_through_and_is_idempotent():
    client, _ = client_with({"claude-opus-5-5": ["ok"]})
    assert client.messages.count_tokens() == 42 and client.api_key == "k"
    assert lm.wrap_client(client) is client and lm.wrap_client(None) is None


# â”€â”€ pricing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_prices_match_the_price_sheet():
    # 1M in + 1M out
    assert lm.calculate_cost(1_000_000, 1_000_000, "claude-opus-5-5") == pytest.approx(4 + 20)
    assert lm.calculate_cost(1_000_000, 1_000_000, "claude-opus-4-8") == pytest.approx(5 + 25)
    assert lm.calculate_cost(1_000_000, 1_000_000, "claude-opus-4-7") == pytest.approx(30)
    assert lm.calculate_cost(1_000_000, 1_000_000, "claude-sonnet-5-5") == pytest.approx(2 + 10)
    assert lm.calculate_cost(1_000_000, 1_000_000, "claude-haiku-4-5-20251001") == pytest.approx(1 + 5)
    # cache: opus-5-5 write 5, read 0.2 per 1M
    assert lm.calculate_cost(0, 0, "claude-opus-5-5", cache_write_tokens=1_000_000, cache_read_tokens=1_000_000) \
        == pytest.approx(5.2)


def test_primary_is_cheaper_than_the_old_default_and_unknown_models_are_not_under_billed():
    old = lm.calculate_cost(500_000, 100_000, "claude-opus-4-7")
    new = lm.calculate_cost(500_000, 100_000, "claude-opus-5-5")
    assert new < old and new == pytest.approx(0.8 * old)
    # an unlisted model is costed at the top rate, never silently cheaper
    assert lm.calculate_cost(1_000_000, 1_000_000, "claude-made-up-9") == pytest.approx(30)


def test_usage_summary_lists_models_used_and_resets():
    client, _ = client_with({"claude-opus-5-5": ["a", ApiError(404, "model not found")], "claude-opus-4-8": ["b"]})
    client.messages.create(model="claude-opus-5-5", messages=[])
    client.messages.create(model="claude-opus-5-5", messages=[])
    s = lm.usage_summary()
    assert s["models"] == {"claude-opus-5-5": 1, "claude-opus-4-8": 1} and s["fallback_used"]
    lm.reset_usage()
    assert lm.usage_summary()["models"] == {}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• prompt caching â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

LONG = "State clean prompt. " * 40  # > CACHE_MIN_CHARS (100)


class Recorder:
    """A messages API that records exactly what it was sent, and can reject cache markers."""

    def __init__(self, reject_cache=False, usage=None):
        self.sent, self.reject_cache, self.usage = [], reject_cache, usage

    def _check(self, kw):
        self.sent.append(kw)
        if self.reject_cache and lm.has_cache_control(kw):
            raise ApiError(400, "system.0.cache_control: Extra inputs are not permitted")

    def create(self, **kw):
        self._check(kw)
        return SimpleNamespace(model=kw["model"], usage=self.usage)

    def stream(self, **kw):
        rec = self

        class CM:
            def __enter__(s):
                rec._check(kw)
                return SimpleNamespace(get_final_message=lambda: SimpleNamespace(usage=rec.usage))

            def __exit__(s, *a):
                return False

        return CM()


def rec_client(**kw):
    inner = SimpleNamespace(messages=Recorder(**kw))
    return lm.wrap_client(inner), inner.messages


def test_a_large_system_prompt_is_marked_cacheable_and_everything_else_is_untouched():
    out = lm.apply_cache({"model": "m", "system": LONG, "max_tokens": 5, "messages": []})
    assert out["system"] == [{"type": "text", "text": LONG, "cache_control": {"type": "ephemeral"}}]
    assert out["max_tokens"] == 5 and out["model"] == "m"
    short = {"system": "tiny"}
    assert lm.apply_cache(short) is short                                  # below the minimum
    assert lm.apply_cache({"messages": []}) == {"messages": []}            # no system prompt
    already = {"system": [{"type": "text", "text": LONG}]}
    assert lm.apply_cache(already) is already                              # caller already shaped it


def test_caching_off_by_setting_or_after_rejection_sends_the_plain_string(monkeypatch):
    monkeypatch.setattr(lm, "CACHE_ENABLED", False)
    assert lm.apply_cache({"system": LONG})["system"] == LONG
    monkeypatch.setattr(lm, "CACHE_ENABLED", True)
    monkeypatch.setattr(lm, "_cache_blocked", True)
    assert lm.apply_cache({"system": LONG})["system"] == LONG
    assert lm.cache_breakpoint({"type": "text", "text": "x"}) == {"type": "text", "text": "x"}


def test_the_wrapper_sends_the_marker_to_the_api():
    client, api_ = rec_client()
    client.messages.create(model="claude-opus-5-5", system=LONG, messages=[{"role": "user", "content": "hi"}])
    assert api_.sent[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    with client.messages.stream(model="claude-opus-5-5", system=LONG, messages=[]):
        pass
    assert api_.sent[1]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_a_gateway_that_rejects_cache_markers_is_retried_plain_and_remembered():
    client, api_ = rec_client(reject_cache=True)
    r = client.messages.create(model="claude-opus-5-5", system=LONG, messages=[])
    assert r.model == "claude-opus-5-5"                        # the call still succeeded, same model
    assert lm.has_cache_control(api_.sent[0]) and not lm.has_cache_control(api_.sent[1])
    assert api_.sent[1]["system"] == LONG                      # back to the original plain string
    assert lm._cache_blocked is True and not lm.usage_summary()["fallback_used"]   # not a model problem
    client.messages.create(model="claude-opus-5-5", system=LONG, messages=[])
    assert not lm.has_cache_control(api_.sent[2])              # remembered: no more markers


def test_stream_also_survives_a_cache_rejection():
    client, api_ = rec_client(reject_cache=True)
    with client.messages.stream(model="claude-opus-5-5", system=LONG, messages=[]) as s:
        assert s is not None
    assert [lm.has_cache_control(k) for k in api_.sent] == [True, False]


def test_errors_unrelated_to_caching_are_not_swallowed_and_do_not_switch_caching_off():
    class Boom:
        def create(self, **kw):
            raise ApiError(400, "messages: field required")

    client = lm.wrap_client(SimpleNamespace(messages=Boom()))
    with pytest.raises(ApiError):
        client.messages.create(model="claude-opus-5-5", system=LONG, messages=[])
    assert lm._cache_blocked is False


def test_strip_cache_restores_the_original_request():
    marked = lm.apply_cache({"system": LONG, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "b"}]}]})
    plain = lm.strip_cache(marked)
    assert plain["system"] == LONG and not lm.has_cache_control(plain)
    assert plain["messages"][0]["content"] == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• cost ledger â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def U(inp=0, out=0, write=0, read=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=write, cache_read_input_tokens=read)


def test_ledger_prices_cache_writes_and_reads_from_the_price_sheet():
    client, _ = rec_client(usage=U(inp=1000, out=200, write=7000))
    client.messages.create(model="claude-opus-5-5", system=LONG, messages=[])
    # opus-5-5: $4 in, $20 out, $5 cache-write, $0.20 cache-read per 1M
    assert lm.ledger_totals()["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20 + 7000 * 5) / 1e6)
    client2, _ = rec_client(usage=U(inp=1000, out=200, read=7000))
    client2.messages.create(model="claude-opus-5-5", system=LONG, messages=[])
    t = lm.ledger_totals()
    assert t["api_calls"] == 2 and t["cache_write_tokens"] == 7000 and t["cache_read_tokens"] == 7000
    assert t["cost_usd"] == pytest.approx(0.043 + (1000 * 4 + 200 * 20 + 7000 * 0.2) / 1e6)
    # the write cost $7000 more-than-plain-input... the read saved $3.80/M: net saving across both calls
    assert t["cache_saving_usd"] == pytest.approx(-7000 * 1 / 1e6 + 7000 * 3.8 / 1e6)


def test_caching_economics_for_a_job_shaped_like_the_FL_bill():
    """~13k tokens in (7k of it the state prompt), ~1.7k out, on claude-opus-5-5.

    Caching costs a little on the FIRST document (the write premium) and pays back from the SECOND
    document of that state inside the cache lifetime. This pins the numbers quoted to the user.
    """
    def job_cost(usage):
        lm.ledger_reset()
        c, _ = rec_client(usage=usage)
        c.messages.create(model="claude-opus-5-5", messages=[])
        return lm.ledger_totals()["cost_usd"]

    no_cache = job_cost(U(inp=12_987, out=1_730))                      # caching off / not applicable
    first = job_cost(U(inp=5_987, out=1_730, write=7_000))            # cold: writes the 7k-token prompt
    warm = job_cost(U(inp=5_987, out=1_730, read=7_000))              # next doc, same state, within 5 min

    assert no_cache == pytest.approx(0.086548, abs=1e-6)
    assert first == pytest.approx(0.093548, abs=1e-6) and first > no_cache      # +8%: the write premium
    assert warm == pytest.approx(0.059948, abs=1e-6) and warm < no_cache * 0.70  # -31% on every hit
    assert first + warm < 2 * no_cache                                          # ahead after just two documents


def test_phases_keep_conversion_cost_separate_from_validation_cost():
    client, _ = rec_client(usage=U(inp=1_000_000, out=0))           # $4.00 on opus-5-5
    client.messages.create(model="claude-opus-5-5", messages=[])     # default phase: conversion
    assert lm.ledger_totals("validation")["api_calls"] == 0          # nothing validated yet -> nothing to show
    with lm.phase("validation"):
        client.messages.create(model="claude-opus-5-5", messages=[])
        with client.messages.stream(model="claude-opus-5-5", messages=[]):
            pass
    assert lm.ledger_totals("conversion")["api_calls"] == 1
    assert lm.ledger_totals("conversion")["cost_usd"] == pytest.approx(4.0)
    assert lm.ledger_totals("validation")["api_calls"] == 2           # the create + the stream
    assert lm.ledger_totals()["api_calls"] == 3
    with lm.phase("validation"):
        pass
    client.messages.create(model="claude-opus-5-5", messages=[])
    assert lm.ledger_totals("conversion")["api_calls"] == 2           # phase restored after the block


def test_a_stream_that_raises_is_not_billed_to_the_ledger():
    client, _ = rec_client(usage=U(inp=10, out=10))
    with pytest.raises(RuntimeError):
        with client.messages.stream(model="claude-opus-5-5", messages=[]):
            raise RuntimeError("caller blew up mid-stream")
    assert lm.ledger_totals()["api_calls"] == 0


def test_ledger_records_the_model_that_actually_answered_and_its_price():
    client, _ = client_with({"claude-opus-5-5": [ApiError(404, "model not found")], "claude-opus-4-8": ["ok"]})
    inner = client.raw.messages
    inner.create_orig = inner.create

    def with_usage(**kw):
        r = inner.create_orig(**kw)
        r.usage = U(inp=1_000_000, out=0)
        return r

    inner.create = with_usage
    client.messages.create(model="claude-opus-5-5", messages=[])
    t = lm.ledger_totals()
    assert t["model"] == "claude-opus-4-8" and t["cost_usd"] == pytest.approx(5.0)   # 4.8 price, not 5.5's $4


def test_ledger_reset_clears_and_empty_totals_are_zero():
    assert lm.ledger_totals("validation") == {
        "api_calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": 0.0, "cache_saving_usd": 0.0, "model": "", "models": {}}


def test_response_text_skips_thinking_blocks():
    from types import SimpleNamespace as NS
    resp = NS(content=[NS(type='thinking', thinking='hmm'), NS(type='text', text='<L>ok</L>')])
    assert lm.response_text(resp) == '<L>ok</L>'



# ── auto mode: cache only when something will read it ─────────────────────────

def test_auto_mode_does_not_cache_a_lone_document(monkeypatch):
    monkeypatch.setattr(lm, "CACHE_AUTO", True)
    assert lm.apply_cache({"system": LONG})["system"] == LONG
    ok, why = lm.cache_decision()
    assert ok is False and "lone" in why


def test_auto_mode_caches_when_more_jobs_for_the_state_are_queued(monkeypatch):
    monkeypatch.setattr(lm, "CACHE_AUTO", True)
    with lm.cache_hint(True):
        assert lm.has_cache_control(lm.apply_cache({"system": LONG}))
        assert lm.cache_decision() == (True, "more jobs for this state are queued")
    assert lm.cache_decision(key="another-prompt")[0] is False   # the hint is gone again


def test_auto_mode_keeps_caching_while_the_cache_is_warm_then_stops(monkeypatch):
    monkeypatch.setattr(lm, "CACHE_AUTO", True)
    clock = {"t": 1000.0}
    monkeypatch.setattr(lm.time, "monotonic", lambda: clock["t"])
    with lm.cache_hint(True):
        lm.apply_cache({"system": LONG})              # first call writes the cache
    clock["t"] += 100
    assert lm.has_cache_control(lm.apply_cache({"system": LONG}))        # a later lone job reads it for free
    clock["t"] += lm.CACHE_WARM_S + 1
    assert lm.apply_cache({"system": LONG})["system"] == LONG            # gone cold: back to plain
    assert lm.cache_decision(key="never-seen")[0] is False


def test_auto_mode_is_per_prompt(monkeypatch):
    monkeypatch.setattr(lm, "CACHE_AUTO", True)
    with lm.cache_hint(True):
        lm.apply_cache({"system": LONG})
    other = LONG + " a different state prompt"
    assert lm.apply_cache({"system": other})["system"] == other


def test_master_switch_beats_the_hint(monkeypatch):
    monkeypatch.setattr(lm, "CACHE_AUTO", True)
    monkeypatch.setattr(lm, "CACHE_ENABLED", False)
    with lm.cache_hint(True):
        assert lm.apply_cache({"system": LONG})["system"] == LONG
