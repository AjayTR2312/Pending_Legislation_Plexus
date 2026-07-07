"""
llm_diff_verifier.py — LLM-based verification of residual text-accuracy diffs.
============================================================================

Background
----------
The text-accuracy pipeline in :func:`compare_files.compare_texts` produces a
list of ``real_diffs`` after running:

  1. Pre-pre-filters (line-number stripping, table-row markers, etc.)
  2. Line-level allowlist (page markers, headers, footers, decorative fill)
  3. Word-level diff (``difflib.SequenceMatcher``)
  4. Word-level allowlist (``classify_removed_text``)

For known formats this catches ~95-100% of expected artifacts.  But when a
**new** state ships a redesigned PDF (or a familiar state introduces a new
footer / header variant), those artifacts fall through every regex layer and
end up as ``real_diffs`` — i.e. false-positive accuracy errors.

This module adds a **fifth, vision-based layer** that runs only when the
regex layers leave residual diffs.  It:

  • Sends the residual ``real_diffs`` list to Claude Opus 4.7 along with the
    same highlighted page images already loaded for the tag-accuracy audit.
  • Asks the model "for each diff, is this real legislative content, or a
    page artifact (header / footer / decorative element / boilerplate)?"
  • Returns a structured verdict per diff: ``category``, ``confidence``,
    ``reasoning``.

Policy applied by :func:`apply_llm_verifications_to_report`
------------------------------------------------------------
**Two-tier safety model** (the user's "third hybrid" choice):

  AUTO-DEMOTE (diff moves from real_diffs → allowed_diffs silently)
    Requires ALL of the following:
      - verdict       == "artifact"
      - confidence    == "high"
      - category      ∈ {header, footer, decorative_rule, page_number}
      - word_count    ≤ 15

  REVIEW-AID ONLY (diff stays in real_diffs, but gets an LLM suggestion)
    Everything else — including:
      - confidence    in {low, medium}
      - category      in {boilerplate, repeated_artifact, real_content, unclear}
      - word_count    > 15 (too long to safely auto-demote even for headers)

The LLM can therefore **only DEMOTE, never PROMOTE**: a real_diff stays a
real_diff unless every safety guard passes.  Even when guards pass, the
auto-demote decision is fully audited (``llm_*`` fields are written to the
diff dict) so a human reviewer can re-promote it later.

Usage
-----
::

    from llm_diff_verifier import verify_real_diffs_with_llm, apply_llm_verifications_to_report

    # report = compare_texts(raw, clean, state_code="IN")
    # if report["real_diffs"]:
    verifications = verify_real_diffs_with_llm(
        real_diffs=report["real_diffs"],
        page_images_b64=hi_images_b64,
        state_code="IN",
    )
    report = apply_llm_verifications_to_report(report, verifications)

The helper is safe to call with empty / zero-length inputs (returns ``[]``)
and degrades gracefully on LLM errors (returns ``[]`` and leaves the report
untouched).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Sequence

log = logging.getLogger("llm_diff_verifier")

# ─────────────────────────────────────────────────────────────────────
# Public constants — the safety policy
# ─────────────────────────────────────────────────────────────────────

# Categories the LLM is allowed to use.  ``real_content`` is the
# explicit "do NOT demote" verdict; ``unclear`` is the LLM's escape
# hatch when it cannot decide.
ALL_CATEGORIES = (
    "header",              # page header (state name, bill title repeats on p2+)
    "footer",              # page footer (page numbers, bill code stamps)
    "decorative_rule",     # underscore lines, dash separators, decorative rules
    "page_number",         # standalone page number ("2", "Page 3")
    "boilerplate",         # printing-code / legal boilerplate
    "repeated_artifact",   # something extracted twice (cross-page bleed)
    "real_content",        # actual legislative text — DO NOT demote
    "unclear",             # LLM uncertain — DO NOT demote
)

# Only these categories are eligible for silent auto-demote.
# Boilerplate + repeated_artifact are intentionally NOT here — they
# require human review because the LLM has historically been wrong
# about what counts as boilerplate in legislative text.
AUTO_DEMOTE_CATEGORIES = frozenset({
    "header",
    "footer",
    "decorative_rule",
    "page_number",
})

# Confidence levels.  Only ``high`` triggers auto-demote.
CONFIDENCE_LEVELS = ("low", "medium", "high")

# Hard word-count cap for auto-demote.  Any diff longer than this stays
# in ``real_diffs`` regardless of LLM verdict — a 30-word "header" is
# almost certainly real legislative text the LLM misread.
MAX_AUTO_DEMOTE_WORDS = 15

# Anthropic model used for verification.  Same as the audit pipeline so
# we can reuse the token-tracking infrastructure.
DEFAULT_MODEL = "claude-opus-4-7"

# Cap on the number of diffs sent in one LLM call.  Anything over this
# is split into batches to keep prompt size sane and to avoid blowing
# the model's response window.
MAX_DIFFS_PER_BATCH = 40

# Cap on page-image count per call.  When more pages exist than this,
# we send a representative subset (first N + last 2 — page artifacts
# usually live on the cover and final pages).  Tuned to keep total
# image tokens under ~30k.
MAX_IMAGES_PER_CALL = 12


# ─────────────────────────────────────────────────────────────────────
# Prompt template
# ─────────────────────────────────────────────────────────────────────

_VERIFIER_PROMPT = """You are a legislative-text-accuracy verifier.

CONTEXT
=======
A deterministic text-comparison pipeline compared the raw PyMuPDF extraction
of a bill (state: {state_code}) against the LLM-cleaned output.  After running
all known artifact-stripping regex filters, the pipeline still flagged
{n_diffs} differences as "missing-from-clean" or "extra-in-clean".

YOUR JOB
========
For EACH numbered diff below, decide whether it is:

  (A) A PAGE ARTIFACT that the cleaner correctly dropped or that does not
      belong to the bill's legislative body.  Examples: page headers
      (the bill title repeated on page 2+), page footers (page numbers,
      bill-code stamps like "2025 IN 1701—LS 7775/DI 92"), decorative
      rules (underscore lines, dashed separators), standalone page
      numbers, printing-code boilerplate, repeated artifacts from
      cross-page bleed.

  (B) REAL LEGISLATIVE CONTENT that the cleaner accidentally dropped or
      added.  This is what we want to surface to the human reviewer.

GROUND TRUTH
============
You are looking at {n_images} highlighted page images from the source PDF.
Use them to decide WHERE on the page the text appears:

  - Top margin (above the body) → likely header
  - Bottom margin (below body) → likely footer
  - In the body text flow → real content
  - Standalone short string near a margin → likely page number or stamp
  - Underscores / dashes spanning the page width → decorative rule

CATEGORIES (use EXACTLY one of these)
======================================
  header             — page header (bill title repeats, state name banner)
  footer             — page footer (page X of Y, bill-code stamp)
  decorative_rule    — underscore / dash separator line
  page_number        — standalone page number
  boilerplate        — printing-code / legal boilerplate (NOT legislative)
  repeated_artifact  — duplicated extraction from page-break bleed
  real_content       — genuine legislative text, NOT an artifact
  unclear            — cannot decide from the available evidence

CONFIDENCE (use EXACTLY one of these)
======================================
  high    — visually confirmed in margin / matches a known artifact pattern
  medium  — likely an artifact but body-vs-margin location ambiguous
  low     — could go either way; reviewer needed

DECISION RULES (CRITICAL)
==========================
1. When in doubt, return ``real_content`` — false positives (calling real
   content an artifact) are MUCH worse than false negatives.
2. If the text is longer than ~10 words, it is probably real_content
   unless it is clearly a multi-line decorative rule.
3. If you cannot find the text in any image, return ``unclear``.
4. Only use ``high`` confidence when the position on the page is
   unambiguously a header / footer / decorative element.

DIFFS TO VERIFY
================
{diffs_block}

OUTPUT
======
Respond with ONLY a JSON object — no markdown, no commentary, no code fence:

{{
  "verifications": [
    {{
      "diff_id": <int — the # number from the diff list>,
      "category": "<one of the categories above>",
      "confidence": "<low | medium | high>",
      "reasoning": "<one short sentence — what you saw, where>"
    }},
    ...
  ]
}}

You MUST return exactly one verification per diff_id.  Do not skip any.
"""


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _diff_text(diff: Dict) -> str:
    """Return the most representative text for a diff dict."""
    return (
        diff.get("missing_text")
        or diff.get("extra_text")
        or diff.get("pymupdf_text")
        or diff.get("converted_text")
        or ""
    )


def _diff_word_count(diff: Dict) -> int:
    """Count words in the most representative text of a diff."""
    return len(_diff_text(diff).split())


def _format_diff_line(idx: int, diff: Dict) -> str:
    """Render a single diff as a one-line entry for the prompt."""
    dtype = diff.get("type", "?")
    text = _diff_text(diff)
    # Truncate very long diffs so the prompt stays readable.  We're
    # only asking the LLM for a verdict — full context isn't needed.
    if len(text) > 240:
        text = text[:237] + "..."
    sentence = (
        diff.get("sentence")
        or diff.get("sentence_pymupdf")
        or diff.get("sentence_converted")
        or ""
    )
    if sentence and len(sentence) > 200:
        sentence = sentence[:197] + "..."

    parts = [f"#{idx} [{dtype}] {text!r}"]
    if sentence and sentence.strip() not in {text.strip(), ""}:
        parts.append(f"    context: {sentence!r}")
    return "\n".join(parts)


def _select_pages_for_call(
    page_images_b64: Sequence[str],
    max_images: int = MAX_IMAGES_PER_CALL,
) -> List[str]:
    """Pick a representative subset of pages when there are too many.

    Strategy: keep page 1 (cover, common header source), the last 2
    pages (footer / signature lines), and as many evenly-spaced middle
    pages as the budget allows.
    """
    n = len(page_images_b64)
    if n <= max_images:
        return list(page_images_b64)

    # Always include page 1 and the last 2.
    must_have = {0, n - 2, n - 1}
    # Fill the rest with evenly-spaced indices from the body.
    remaining_budget = max_images - len(must_have)
    if remaining_budget > 0 and n > 3:
        step = max(1, (n - 3) // remaining_budget)
        for i in range(1, n - 2, step):
            must_have.add(i)
            if len(must_have) >= max_images:
                break

    chosen_idx = sorted(must_have)[:max_images]
    return [page_images_b64[i] for i in chosen_idx]


def _build_verifier_messages(
    real_diffs: Sequence[Dict],
    page_images_b64: Sequence[str],
    state_code: Optional[str],
) -> List[Dict]:
    """Build the Anthropic ``messages`` payload for a verification call."""
    chosen_imgs = _select_pages_for_call(page_images_b64)

    content_parts: List[Dict] = []
    for i, img_b64 in enumerate(chosen_imgs):
        content_parts.append({
            "type": "text",
            "text": f"[Highlighted page sample {i + 1} of {len(chosen_imgs)}]",
        })
        content_parts.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            },
        })

    diffs_block = "\n".join(
        _format_diff_line(idx, d) for idx, d in enumerate(real_diffs)
    )
    prompt = _VERIFIER_PROMPT.format(
        state_code=(state_code or "??").upper(),
        n_diffs=len(real_diffs),
        n_images=len(chosen_imgs),
        diffs_block=diffs_block,
    )
    content_parts.append({"type": "text", "text": prompt})
    return [{"role": "user", "content": content_parts}]


def _coerce_verification(
    raw: Dict,
    n_diffs: int,
) -> Optional[Dict]:
    """Normalise + validate one verification dict from the LLM response.

    Returns ``None`` when the entry is malformed (caller skips it).
    """
    try:
        diff_id = int(raw.get("diff_id"))
    except (TypeError, ValueError):
        return None
    if not (0 <= diff_id < n_diffs):
        return None

    category = str(raw.get("category", "unclear")).strip().lower()
    if category not in ALL_CATEGORIES:
        # Unknown category → treat as ``unclear`` so the safety policy
        # naturally blocks any auto-demote.
        category = "unclear"

    confidence = str(raw.get("confidence", "low")).strip().lower()
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "low"

    reasoning = str(raw.get("reasoning", "")).strip()
    if len(reasoning) > 500:
        reasoning = reasoning[:497] + "..."

    # Derive the verdict from the category — the user's safety model
    # never trusts the LLM to set this directly.
    if category == "real_content":
        verdict = "real_content"
    elif category == "unclear":
        verdict = "unclear"
    else:
        verdict = "artifact"

    return {
        "diff_id": diff_id,
        "verdict": verdict,
        "category": category,
        "confidence": confidence,
        "reasoning": reasoning,
    }


# ─────────────────────────────────────────────────────────────────────
# Main entry — call the LLM
# ─────────────────────────────────────────────────────────────────────

def verify_real_diffs_with_llm(
    real_diffs: Sequence[Dict],
    page_images_b64: Sequence[str],
    state_code: Optional[str] = None,
    *,
    model: str = DEFAULT_MODEL,
    client=None,
    log_callable=None,
) -> List[Dict]:
    """Ask Claude Opus 4.7 to classify each ``real_diff`` as artifact or real.

    Parameters
    ----------
    real_diffs
        List of diff dicts from :func:`compare_files.compare_texts`.  Each
        dict must have a ``type`` field plus one of
        ``missing_text`` / ``extra_text`` / ``pymupdf_text`` / ``converted_text``.
    page_images_b64
        Base64-encoded PNGs of the highlighted PDF pages (the same images
        already loaded for the tag-accuracy audit).  When this list is empty,
        the function returns immediately — no images means no visual evidence.
    state_code
        Two-letter state code (purely informational — passed to the prompt).
    model
        Anthropic model id.  Default :data:`DEFAULT_MODEL`.
    client
        Optional pre-built anthropic.Anthropic client.  When ``None``, one is
        obtained via :func:`gpt_compare._get_anthropic_client`.
    log_callable
        Optional callable for pipeline-local logging (``add_log`` from
        streamlit_app.py).  When ``None``, the module logger is used.

    Returns
    -------
    list[dict]
        One verification dict per diff with keys
        ``diff_id``, ``verdict``, ``category``, ``confidence``, ``reasoning``.
        Returns ``[]`` on any error, empty inputs, or model failure.
        Each ``diff_id`` is the 0-based index into the input ``real_diffs``.

    Notes
    -----
    * Diffs are processed in batches of :data:`MAX_DIFFS_PER_BATCH` to keep
      prompt size manageable.
    * Page images are sampled down to :data:`MAX_IMAGES_PER_CALL` when
      necessary (cover + last 2 + evenly-spaced middle).
    * On any exception the function returns ``[]`` — the caller's pipeline
      continues unmodified.  Verification is a **best-effort enhancement**
      and never blocks the main accuracy report.
    """
    def _log(msg: str, level: str = "info"):
        if log_callable is not None:
            try:
                log_callable(msg)
                return
            except Exception:
                pass
        getattr(log, level, log.info)(msg)

    if not real_diffs:
        _log("LLM verifier: no real_diffs to verify — skipping.")
        return []
    if not page_images_b64:
        _log("LLM verifier: no page images supplied — skipping (no visual evidence).",
             "warning")
        return []

    # Build the client lazily so unit tests can inject mocks.
    if client is None:
        try:
            from gpt_compare import _get_anthropic_client
            client = _get_anthropic_client()
        except Exception as e:
            _log(f"LLM verifier: could not get Anthropic client ({e}) — skipping.",
                 "warning")
            return []

    all_verifications: List[Dict] = []
    n_total = len(real_diffs)

    # Slice into batches.  We preserve a stable global diff_id by passing
    # offset → re-mapping the LLM's local ids into global ones.
    for batch_start in range(0, n_total, MAX_DIFFS_PER_BATCH):
        batch = list(real_diffs[batch_start:batch_start + MAX_DIFFS_PER_BATCH])
        if not batch:
            continue

        _log(
            f"🤖 LLM verifier: classifying diffs "
            f"{batch_start + 1}-{batch_start + len(batch)} of {n_total} "
            f"with {model} (state={state_code}, "
            f"{len(page_images_b64)} pages available)..."
        )

        messages = _build_verifier_messages(batch, page_images_b64, state_code)
        try:
            raw_response = ""
            with client.messages.stream(
                model=model,
                max_tokens=8000,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    raw_response += text
                final_msg = stream.get_final_message()
            raw_response = raw_response.strip()
        except Exception as e:
            _log(f"LLM verifier: API call failed ({e}) — skipping batch.",
                 "warning")
            continue

        # Token tracking — mirror the pattern used by pymupdf_compare_tags.
        try:
            import sys as _sys
            _main_mod = _sys.modules.get("__main__")
            if _main_mod and hasattr(_main_mod, "_track_usage"):
                _main_mod._track_usage("LLM Diff Verifier",
                                       final_msg.usage, model)
        except Exception:
            pass

        # Strip leading / trailing code fences (defensive).
        if raw_response.startswith("```"):
            raw_response = "\n".join(
                ln for ln in raw_response.split("\n")
                if not ln.strip().startswith("```")
            )

        # Parse JSON with repair fallback.
        try:
            result = json.loads(raw_response)
        except json.JSONDecodeError:
            try:
                from gpt_compare import _repair_json
                result = json.loads(_repair_json(raw_response))
            except Exception as e:
                _log(f"LLM verifier: JSON parse failed ({e}) — skipping batch.",
                     "warning")
                continue

        batch_verifications = result.get("verifications", []) or []
        n_kept = 0
        for raw in batch_verifications:
            normalised = _coerce_verification(raw, n_diffs=len(batch))
            if normalised is None:
                continue
            # Remap the batch-local diff_id back to a global one.
            normalised["diff_id"] = normalised["diff_id"] + batch_start
            all_verifications.append(normalised)
            n_kept += 1

        _log(
            f"   ↳ batch returned {n_kept}/{len(batch)} verification(s)."
        )

    _log(
        f"✅ LLM verifier: classified {len(all_verifications)}/{n_total} "
        f"real_diff(s) ({_count_by(all_verifications, 'verdict')})."
    )
    return all_verifications


def _count_by(items: Sequence[Dict], key: str) -> str:
    """Tiny helper: human-readable counts grouped by a key."""
    counts: Dict[str, int] = {}
    for it in items:
        v = str(it.get(key, "?"))
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return "none"
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items(),
                                                     key=lambda kv: -kv[1]))


# ─────────────────────────────────────────────────────────────────────
# Apply policy — auto-demote vs review-aid
# ─────────────────────────────────────────────────────────────────────

def is_auto_demote_eligible(
    diff: Dict,
    verification: Dict,
    *,
    max_words: int = MAX_AUTO_DEMOTE_WORDS,
) -> bool:
    """Return True when the diff passes ALL auto-demote safety checks.

    The four guards (all must hold):
      1. verification["verdict"]    == "artifact"
      2. verification["confidence"] == "high"
      3. verification["category"]   ∈ AUTO_DEMOTE_CATEGORIES
      4. word_count(diff)           ≤ max_words

    Any failure → return False → caller keeps the diff in ``real_diffs``
    and just attaches the LLM hint as a review aid.
    """
    if verification.get("verdict") != "artifact":
        return False
    if verification.get("confidence") != "high":
        return False
    if verification.get("category") not in AUTO_DEMOTE_CATEGORIES:
        return False
    if _diff_word_count(diff) > max_words:
        return False
    return True


def apply_llm_verifications_to_report(
    report: Dict,
    verifications: Sequence[Dict],
    *,
    log_callable=None,
) -> Dict:
    """Apply the auto-demote vs review-aid policy to a ``compare_texts`` report.

    Mutates ``report`` IN PLACE and also returns it.  Idempotent: calling
    twice with the same verifications produces the same final report.

    Effects on the report:
      • Diffs that pass :func:`is_auto_demote_eligible` are removed from
        ``real_diffs`` and appended to ``allowed_diffs`` with
        ``allowed_category = "llm_<llm_category>"``,
        ``allowed_rule     = "LLM-verified <category> (auto-demoted)"``,
        and ``auto_demoted  = True``.
      • Remaining real_diffs whose LLM verdict is ``artifact`` (but didn't
        clear all safety guards) get the LLM suggestion attached as
        ``llm_suggestion`` (review aid only — verdict unchanged).
      • All affected diffs get the raw ``llm_verdict / llm_category /
        llm_confidence / llm_reasoning`` fields written for full audit trail.
      • Accuracy is recomputed: auto-demoted words bonus back into
        ``matching_words`` (mirrors the existing allowlist bonus logic).
      • Status is recomputed: if ``real_diffs`` is now empty, status
        becomes ``CONTENT_MATCH``.
      • New report fields:
          ``llm_verification_applied`` — always True after this call
          ``llm_verified_count``       — total diffs the LLM classified
          ``llm_auto_demoted_count``   — diffs silently moved to allowed_diffs
          ``llm_review_aid_count``     — diffs kept as real with suggestion
          ``llm_categories``           — dict[category, count] across all verdicts
    """
    def _log(msg: str):
        if log_callable is not None:
            try:
                log_callable(msg)
                return
            except Exception:
                pass
        log.info(msg)

    # Build a lookup: diff_id → verification.  We treat diff_id as the
    # 0-based index into the ORIGINAL real_diffs list (before this call).
    ver_by_id: Dict[int, Dict] = {}
    for v in verifications:
        try:
            ver_by_id[int(v["diff_id"])] = v
        except (KeyError, TypeError, ValueError):
            continue

    orig_real_diffs: List[Dict] = list(report.get("real_diffs", []))
    new_real_diffs: List[Dict] = []
    new_allowed: List[Dict] = list(report.get("allowed_diffs", []))
    allowed_categories: Dict[str, int] = dict(report.get("allowed_categories", {}))

    auto_demoted: List[Dict] = []
    review_aid_count = 0
    llm_categories: Dict[str, int] = {}

    for idx, diff in enumerate(orig_real_diffs):
        ver = ver_by_id.get(idx)
        if ver is None:
            # No verdict for this one — leave untouched.
            new_real_diffs.append(diff)
            continue

        # Track category distribution for the summary regardless of action.
        cat = ver.get("category", "unclear")
        llm_categories[cat] = llm_categories.get(cat, 0) + 1

        # Stamp the LLM metadata on a COPY so we never mutate the
        # caller's original dict.
        diff_out = dict(diff)
        diff_out["llm_verdict"]     = ver.get("verdict", "unclear")
        diff_out["llm_category"]    = cat
        diff_out["llm_confidence"]  = ver.get("confidence", "low")
        diff_out["llm_reasoning"]   = ver.get("reasoning", "")

        if is_auto_demote_eligible(diff, ver):
            # SILENT AUTO-DEMOTE → moves to allowed_diffs.
            diff_out["auto_demoted"]     = True
            diff_out["allowed_category"] = f"llm_{cat}"
            diff_out["allowed_rule"]     = (
                f"LLM-verified {cat} (auto-demoted, high confidence, "
                f"{_diff_word_count(diff)} words)"
            )
            new_allowed.append(diff_out)
            auto_demoted.append(diff_out)
            allowed_categories[f"llm_{cat}"] = allowed_categories.get(
                f"llm_{cat}", 0
            ) + 1
        else:
            # REVIEW AID ONLY → stays in real_diffs.
            diff_out["auto_demoted"] = False
            if ver.get("verdict") == "artifact":
                diff_out["llm_suggestion"] = (
                    f"LLM thinks this is a {cat} ({ver.get('confidence', 'low')} "
                    f"confidence). Click to mark allowed if you agree."
                )
                review_aid_count += 1
            new_real_diffs.append(diff_out)

    # ── Recompute accuracy bonus from auto-demoted diffs ────────────
    if auto_demoted:
        bonus_words = 0
        for d in auto_demoted:
            dtype = d.get("type")
            if dtype == "REPLACE":
                bonus_words += max(
                    len((d.get("pymupdf_text") or "").split()),
                    len((d.get("converted_text") or "").split()),
                )
            elif dtype == "MISSING_IN_CONVERTED":
                bonus_words += len((d.get("missing_text") or "").split())
            elif dtype == "EXTRA_IN_CONVERTED":
                bonus_words += len((d.get("extra_text") or "").split())

        max_words = max(
            report.get("pymupdf_word_count", 0),
            report.get("converted_word_count", 0),
        )
        if max_words > 0 and bonus_words > 0:
            new_matching = report.get("matching_words", 0) + bonus_words
            report["matching_words"] = new_matching
            report["accuracy"] = round(new_matching / max_words * 100, 2)

    # ── Recompute status: if no real diffs left, content matches ────
    if not new_real_diffs and report.get("status") == "DIFFERENT":
        report["status"] = "CONTENT_MATCH"

    # ── Write the updated lists + counters back ──────────────────────
    report["real_diffs"]         = new_real_diffs
    report["real_diff_count"]    = len(new_real_diffs)
    report["allowed_diffs"]      = new_allowed
    report["allowed_diff_count"] = len(new_allowed)
    report["allowed_categories"] = allowed_categories

    # Recount sub-types after demotion.
    n_trans = sum(1 for d in new_real_diffs if d.get("sub_type") == "TRANSPOSED")
    n_punct = sum(1 for d in new_real_diffs if d.get("sub_type") == "PUNCTUATION")
    report["transposed_count"] = n_trans
    report["punctuation_count"] = n_punct
    report["word_diff_count"]   = len(new_real_diffs) - n_trans - n_punct

    # ── New LLM-verification fields ──────────────────────────────────
    report["llm_verification_applied"] = True
    report["llm_verified_count"]       = sum(
        1 for v in verifications if v.get("diff_id") is not None
    )
    report["llm_auto_demoted_count"]   = len(auto_demoted)
    report["llm_review_aid_count"]     = review_aid_count
    report["llm_categories"]           = llm_categories

    _log(
        f"🤖 LLM policy: {len(auto_demoted)} auto-demoted "
        f"(silent → allowed_diffs), {review_aid_count} review-aid "
        f"(stayed as real_diffs with suggestion). "
        f"Final accuracy: {report.get('accuracy', 0):.2f}%."
    )
    return report


__all__ = [
    "ALL_CATEGORIES",
    "AUTO_DEMOTE_CATEGORIES",
    "CONFIDENCE_LEVELS",
    "MAX_AUTO_DEMOTE_WORDS",
    "MAX_DIFFS_PER_BATCH",
    "MAX_IMAGES_PER_CALL",
    "DEFAULT_MODEL",
    "verify_real_diffs_with_llm",
    "apply_llm_verifications_to_report",
    "is_auto_demote_eligible",
]
