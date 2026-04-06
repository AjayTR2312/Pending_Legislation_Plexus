"""
Validation.py
=============
LLM-as-a-Judge Validation for Claude Opus 4.6 Legislative PDF Conversion.

Takes the final converted output and the original PDF, renders PDF pages as
images, and sends both to an LLM judge (Gemini via TR Vertex AI or GPT via
TR OpenAI gateway) to independently verify conversion quality.

Checks for:
  - Tag accuracy:     Are [A>...<A] and [D>...<D] placed on the correct text?
  - Missed tags:      Formatted text in the PDF that was NOT tagged
  - False tags:       Tags applied to text that is NOT formatted in the PDF
  - Dropped text:     Words/sentences in the PDF that are completely missing
  - Misplaced tags:   Tags on the right text but wrong boundaries
  - Split/merge:      Tags merged or split incorrectly
  - Layout issues:    Wrong line breaks, spacing, or structural problems

Produces an accuracy score (precision, recall, F1) and a detailed report.

Authentication:
    Uses the TR internal AI Platform gateway — no API keys needed.
    Set:  TR_WORKSPACE_ID  or pass via CLI / constructor.

Usage:
    python Validation.py <pdf_path> <converted_txt_path> [--state XX] [--golden <golden_txt>]
    python Validation.py <pdf_path> <converted_txt_path> --state KS --judge gemini
    python Validation.py <pdf_path> <converted_txt_path> --state CT --judge gpt --golden ref.txt

Requires:
    pip install pypdfium2 requests google-cloud-aiplatform openai
"""

import os
import sys
import re
import json
import math
import base64
import logging
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

# ---------------------------------------------------------------------------
# PDF rendering (same approach as Claude_Opus_4.6_Test.py)
# ---------------------------------------------------------------------------
try:
    import pypdfium2 as pdfium
    _PDFIUM_AVAILABLE = True
except ImportError:
    _PDFIUM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Gemini (Vertex AI) — lazy import
# ---------------------------------------------------------------------------
try:
    from google.oauth2.credentials import Credentials as OAuth2Credentials
    import vertexai
    from vertexai.generative_models import GenerativeModel, Image as VertexImage
    _VERTEXAI_AVAILABLE = True
except ImportError:
    _VERTEXAI_AVAILABLE = False

# ---------------------------------------------------------------------------
# OpenAI — lazy import
# ---------------------------------------------------------------------------
try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("Validation")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TR_WORKSPACE_ID       = "AgentTraininJaH4"
_TR_GEMINI_TOKEN_URL  = "https://aiplatform.gcs.int.thomsonreuters.com/v1/gemini/token"
_TR_OPENAI_TOKEN_URL  = "https://aiplatform.gcs.int.thomsonreuters.com/v1/openai/token"

_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_DEFAULT_GPT_MODEL    = "gpt-5.4"

# Resolve paths relative to this file
_THIS_DIR = Path(__file__).resolve().parent
_STATE_TEMPLATE_PATH = _THIS_DIR / "netscan_state_template.json"


# ---------------------------------------------------------------------------
# Token caches
# ---------------------------------------------------------------------------
_gemini_token_cache: Dict[str, dict] = {}
_openai_client_cache: Dict[str, object] = {}


# ---------------------------------------------------------------------------
# State template loader
# ---------------------------------------------------------------------------
def load_state_config(state_code: str) -> Optional[Dict]:
    """Load the state-specific conversion rules from netscan_state_template.json."""
    if not _STATE_TEMPLATE_PATH.exists():
        log.warning(f"State template not found at {_STATE_TEMPLATE_PATH}")
        return None
    try:
        with open(_STATE_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        states = data.get("states", data) if isinstance(data, dict) else data
        if isinstance(states, list):
            for s in states:
                if s.get("abbreviation", "").upper() == state_code.upper():
                    return s
        elif isinstance(states, dict):
            return states.get(state_code.upper())
    except Exception as e:
        log.error(f"Failed to load state template: {e}")
    return None


def describe_state_rules(state_code: str, config: Optional[Dict] = None) -> str:
    """
    Build a human-readable description of what tags mean for this state.
    Used to inform the LLM judge about state-specific formatting rules.
    """
    if config is None:
        config = load_state_config(state_code)

    if not config:
        return (
            f"State: {state_code}\n"
            f"  [A>...<A] = Added text (typically italic or underlined in the PDF)\n"
            f"  [D>...<D] = Deleted text (typically strikethrough in the PDF)\n"
        )

    lines = [f"State: {config.get('state_name', state_code)} ({state_code})"]

    # Addition rules
    add_cfg = config.get("add_markup", {})
    add_formats = []
    visual_add = []       # formatting visible in images (underline, bold, italic)
    text_based_add = []   # formatting NOT visible in images (all_caps, brackets)

    if add_cfg.get("underline"):
        add_formats.append("UNDERLINED text")
        visual_add.append("underline")
    if add_cfg.get("bold"):
        add_formats.append("BOLD text")
        visual_add.append("bold")
    if add_cfg.get("italic"):
        add_formats.append("ITALIC text")
        visual_add.append("italic")
    if add_cfg.get("all_caps"):
        cond = add_cfg.get("all_caps_condition", "")
        add_formats.append(f"ALL CAPS text" + (f" ({cond})" if cond else ""))
        text_based_add.append("ALL CAPS")
    if add_cfg.get("brackets"):
        add_formats.append("text in BRACKETS")
        text_based_add.append("BRACKETS")
    lines.append(f"  [A>...<A] tags = ADDED language, identified by: {', '.join(add_formats) or 'italic/underline'}")

    # Addition custom rules
    for rule in add_cfg.get("custom_rules", []):
        lines.append(f"    • {rule}")

    # Deletion rules
    del_cfg = config.get("delete_markup", {})
    del_formats = []
    visual_del = []       # formatting visible in images
    text_based_del = []   # formatting NOT visible in images

    if del_cfg.get("strike_out"):
        del_formats.append("STRIKETHROUGH text")
        visual_del.append("strikethrough")
    if del_cfg.get("brackets"):
        del_formats.append("text in [BRACKETS]")
        text_based_del.append("BRACKETS")
    if del_cfg.get("italic"):
        del_formats.append("ITALIC text")
        visual_del.append("italic")
    lines.append(f"  [D>...<D] tags = DELETED language, identified by: {', '.join(del_formats) or 'strikethrough'}")

    for rule in del_cfg.get("custom_rules", []):
        lines.append(f"    • {rule}")

    # Deletions (things to strip)
    deletions = config.get("deletions", "")
    if deletions:
        lines.append(f"  Elements to REMOVE (not tag): {deletions[:200]}")

    # ── Formatting visibility note ──
    # This tells the LLM judge what it can/cannot see in highlighted images
    lines.append("")
    lines.append("  FORMATTING VISIBILITY IN IMAGES:")
    if visual_add or visual_del:
        all_visual = visual_add + visual_del
        lines.append(f"    VISUAL (highlighted in images):     {', '.join(all_visual)}")
        lines.append(f"      → These are colour-coded: Green=underline, Red=strikethrough, Blue=italic, Orange=bold")
    if text_based_add or text_based_del:
        all_text = text_based_add + text_based_del
        lines.append(f"    TEXT-BASED (NOT in images):          {', '.join(all_text)}")
        if "ALL CAPS" in all_text:
            lines.append(f"      → ALL CAPS: Look for UPPERCASE WORDS in the text content, not in the image overlays")
        if "BRACKETS" in all_text:
            lines.append(f"      → BRACKETS: Look for [text in square brackets] in the text content, not in the image overlays")
        lines.append(f"    ⚠️  You MUST validate text-based formatting by reading the actual text, NOT by looking at image highlights")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PDF → base64 PNG images
# ---------------------------------------------------------------------------
def pdf_to_images(pdf_path: str, dpi: int = 300, max_pages: int = 50) -> List[str]:
    """
    Render PDF pages to base64-encoded PNG images.
    Returns a list of base64 strings, one per page.
    """
    if not _PDFIUM_AVAILABLE:
        log.error("pypdfium2 not installed. Run: pip install pypdfium2")
        return []

    images = []
    try:
        doc = pdfium.PdfDocument(pdf_path)
        n_pages = min(len(doc), max_pages)
        log.info(f"📄 Rendering {n_pages} page(s) from PDF at {dpi} DPI...")

        for i in range(n_pages):
            page = doc[i]
            scale = dpi / 72  # 72 is the PDF default DPI
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()

            import io
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            images.append(b64)

        doc.close()
        log.info(f"✅ Rendered {len(images)} page image(s)")
    except Exception as e:
        log.error(f"Failed to render PDF: {e}")

    return images


# ---------------------------------------------------------------------------
# TR Gemini authentication + model
# ---------------------------------------------------------------------------
def _get_gemini_model(workspace_id: str, model_name: str) -> Optional["GenerativeModel"]:
    """Authenticate via TR gateway and return a Vertex AI GenerativeModel."""
    if not _VERTEXAI_AVAILABLE:
        log.error("google-cloud-aiplatform not installed")
        return None

    cached = _gemini_token_cache.get(model_name)
    if cached:
        try:
            expires_on = datetime.fromisoformat(cached["expires_on"])
            if expires_on.tzinfo is None:
                expires_on = expires_on.replace(tzinfo=timezone.utc)
            if (expires_on - datetime.now(timezone.utc)).total_seconds() > 60:
                return cached["model"]
        except Exception:
            pass

    payload = {"workspace_id": workspace_id, "model_name": model_name}
    try:
        resp = requests.post(_TR_GEMINI_TOKEN_URL, json=payload, timeout=30)
        resp.raise_for_status()
        creds = resp.json()
        if "token" not in creds:
            log.error("TR Gemini token response missing 'token' key")
            return None
    except requests.RequestException as e:
        log.error(f"TR Gemini token request failed: {e}")
        return None

    temp_creds = OAuth2Credentials(creds["token"])
    vertexai.init(
        project=creds["project_id"],
        location=creds["region"],
        credentials=temp_creds,
    )
    model = GenerativeModel(model_name)
    _gemini_token_cache[model_name] = {
        "model": model,
        "expires_on": creds.get("expires_on", ""),
    }
    log.info(f"✅ Gemini token obtained (expires {creds.get('expires_on', 'unknown')})")
    return model


# ---------------------------------------------------------------------------
# TR OpenAI authentication + client
# ---------------------------------------------------------------------------
def _get_openai_client(workspace_id: str, model_name: str) -> Optional["OpenAI"]:
    """Authenticate via TR gateway and return an OpenAI client."""
    if not _OPENAI_AVAILABLE:
        log.error("openai package not installed")
        return None

    payload = {
        "workspace_id": workspace_id,
        "model_name": model_name,
        "oai_access": "openai_direct",
    }
    try:
        resp = requests.post(_TR_OPENAI_TOKEN_URL, json=payload, timeout=30)
        resp.raise_for_status()
        creds = resp.json()
    except requests.RequestException as e:
        log.error(f"TR OpenAI token request failed: {e}")
        return None

    if creds.get("is_direct_openai"):
        client = OpenAI(
            api_key=creds["openai_api_key"],
            base_url="https://us.api.openai.com/v1",
        )
    else:
        client = OpenAI(
            api_key=creds.get("openai_api_key", creds.get("token", "")),
            base_url=creds.get("base_url", "https://us.api.openai.com/v1"),
        )
    log.info("✅ OpenAI client obtained")
    return client


# ---------------------------------------------------------------------------
# Structural pre-analysis (regex-based, no LLM needed)
# ---------------------------------------------------------------------------
def structural_analysis(converted_text: str) -> Dict:
    """
    Quick regex-based analysis of the converted output to gather statistics
    before sending to the LLM judge.
    """
    # Count tags
    add_tags = re.findall(r'\[A>(.*?)<A\]', converted_text, re.DOTALL)
    del_tags = re.findall(r'\[D>(.*?)<D\]', converted_text, re.DOTALL)

    # Check for mismatched tags
    open_a  = len(re.findall(r'\[A>', converted_text))
    close_a = len(re.findall(r'<A\]', converted_text))
    open_d  = len(re.findall(r'\[D>', converted_text))
    close_d = len(re.findall(r'<D\]', converted_text))

    # Check for nested tags (should not exist)
    nested = re.findall(r'\[A>.*?\[A>|\[D>.*?\[D>|\[A>.*?\[D>|\[D>.*?\[A>', converted_text, re.DOTALL)

    # Check for empty tags
    empty_a = len(re.findall(r'\[A>\s*<A\]', converted_text))
    empty_d = len(re.findall(r'\[D>\s*<D\]', converted_text))

    # Word count (excluding tags)
    plain = re.sub(r'\[A>|<A\]|\[D>|<D\]', '', converted_text)
    word_count = len(plain.split())

    # Line count
    line_count = len(converted_text.splitlines())

    return {
        "addition_tags": len(add_tags),
        "deletion_tags": len(del_tags),
        "total_tags": len(add_tags) + len(del_tags),
        "mismatched_additions": open_a != close_a,
        "mismatched_deletions": open_d != close_d,
        "open_a": open_a,
        "close_a": close_a,
        "open_d": open_d,
        "close_d": close_d,
        "nested_tags": len(nested),
        "empty_addition_tags": empty_a,
        "empty_deletion_tags": empty_d,
        "word_count": word_count,
        "line_count": line_count,
        "addition_texts": [t.strip()[:80] for t in add_tags[:30]],
        "deletion_texts": [t.strip()[:80] for t in del_tags[:30]],
    }


def golden_comparison(converted_text: str, golden_text: str) -> Dict:
    """
    Compare converted output against a golden reference to find differences.
    Returns a dict of discrepancies for the LLM judge to consider.
    """
    conv_adds = set(re.findall(r'\[A>(.*?)<A\]', converted_text, re.DOTALL))
    gold_adds = set(re.findall(r'\[A>(.*?)<A\]', golden_text,    re.DOTALL))
    conv_dels = set(re.findall(r'\[D>(.*?)<D\]', converted_text, re.DOTALL))
    gold_dels = set(re.findall(r'\[D>(.*?)<D\]', golden_text,    re.DOTALL))

    # Normalize whitespace for comparison
    def norm(s): return " ".join(s.split())
    conv_adds_n = {norm(t) for t in conv_adds}
    gold_adds_n = {norm(t) for t in gold_adds}
    conv_dels_n = {norm(t) for t in conv_dels}
    gold_dels_n = {norm(t) for t in gold_dels}

    missing_adds = gold_adds_n - conv_adds_n   # in golden but not in converted
    extra_adds   = conv_adds_n - gold_adds_n   # in converted but not in golden
    missing_dels = gold_dels_n - conv_dels_n
    extra_dels   = conv_dels_n - gold_dels_n

    # Precision / Recall for additions
    tp_a = len(conv_adds_n & gold_adds_n)
    fp_a = len(extra_adds)
    fn_a = len(missing_adds)

    # Precision / Recall for deletions
    tp_d = len(conv_dels_n & gold_dels_n)
    fp_d = len(extra_dels)
    fn_d = len(missing_dels)

    def safe_div(a, b): return round(a / b, 4) if b > 0 else 0.0

    precision_a = safe_div(tp_a, tp_a + fp_a)
    recall_a    = safe_div(tp_a, tp_a + fn_a)
    f1_a        = safe_div(2 * precision_a * recall_a, precision_a + recall_a) if (precision_a + recall_a) > 0 else 0.0

    precision_d = safe_div(tp_d, tp_d + fp_d)
    recall_d    = safe_div(tp_d, tp_d + fn_d)
    f1_d        = safe_div(2 * precision_d * recall_d, precision_d + recall_d) if (precision_d + recall_d) > 0 else 0.0

    # Overall
    tp_all = tp_a + tp_d
    fp_all = fp_a + fp_d
    fn_all = fn_a + fn_d
    precision_all = safe_div(tp_all, tp_all + fp_all)
    recall_all    = safe_div(tp_all, tp_all + fn_all)
    f1_all        = safe_div(2 * precision_all * recall_all, precision_all + recall_all) if (precision_all + recall_all) > 0 else 0.0

    # Dropped text check (words in golden but not in converted, ignoring tags)
    conv_plain = re.sub(r'\[A>|<A\]|\[D>|<D\]', '', converted_text)
    gold_plain = re.sub(r'\[A>|<A\]|\[D>|<D\]', '', golden_text)
    conv_words = set(conv_plain.lower().split())
    gold_words = set(gold_plain.lower().split())
    dropped_words = gold_words - conv_words
    added_words   = conv_words - gold_words

    return {
        "addition_tags": {
            "golden_count": len(gold_adds),
            "converted_count": len(conv_adds),
            "true_positive": tp_a,
            "false_positive": fp_a,
            "false_negative": fn_a,
            "precision": precision_a,
            "recall": recall_a,
            "f1": f1_a,
            "missing": sorted(list(missing_adds))[:20],
            "extra": sorted(list(extra_adds))[:20],
        },
        "deletion_tags": {
            "golden_count": len(gold_dels),
            "converted_count": len(conv_dels),
            "true_positive": tp_d,
            "false_positive": fp_d,
            "false_negative": fn_d,
            "precision": precision_d,
            "recall": recall_d,
            "f1": f1_d,
            "missing": sorted(list(missing_dels))[:20],
            "extra": sorted(list(extra_dels))[:20],
        },
        "overall": {
            "precision": precision_all,
            "recall": recall_all,
            "f1": f1_all,
        },
        "dropped_word_count": len(dropped_words),
        "extra_word_count": len(added_words),
        "dropped_words_sample": sorted(list(dropped_words))[:30],
    }


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    """Full validation findings from the LLM judge."""
    # Tag accuracy
    missed_additions:  List[Dict] = field(default_factory=list)
    missed_deletions:  List[Dict] = field(default_factory=list)
    false_additions:   List[Dict] = field(default_factory=list)
    false_deletions:   List[Dict] = field(default_factory=list)

    # Content issues
    dropped_text:      List[Dict] = field(default_factory=list)
    misplaced_tags:    List[Dict] = field(default_factory=list)
    split_merge_issues:List[Dict] = field(default_factory=list)
    layout_issues:     List[Dict] = field(default_factory=list)

    # Scores from LLM judge
    tag_accuracy_pct:  float = 0.0   # Overall tag placement accuracy (0-100)
    content_accuracy_pct: float = 0.0  # Text completeness (0-100)
    overall_score_pct: float = 0.0   # Combined quality score (0-100)

    # Page-by-page scores from LLM judge
    page_summaries:    List[Dict] = field(default_factory=list)

    # Per-formatted-segment inventory from LLM judge
    formatted_segments: List[Dict] = field(default_factory=list)

    # Scores from golden comparison (if available)
    golden_precision:  Optional[float] = None
    golden_recall:     Optional[float] = None
    golden_f1:         Optional[float] = None

    # Structural stats
    structural_stats:  Dict = field(default_factory=dict)

    # Metadata
    state_code:   str = ""
    judge_model:  str = ""
    confidence:   str = "medium"
    reasoning:    str = ""
    pdf_pages:    int = 0

    def total_issues(self) -> int:
        return (len(self.missed_additions) + len(self.missed_deletions)
                + len(self.false_additions) + len(self.false_deletions)
                + len(self.dropped_text) + len(self.misplaced_tags)
                + len(self.split_merge_issues) + len(self.layout_issues))

    def grade(self) -> str:
        """Letter grade based on overall score."""
        s = self.overall_score_pct
        if s >= 95: return "A+"
        if s >= 90: return "A"
        if s >= 85: return "B+"
        if s >= 80: return "B"
        if s >= 70: return "C"
        if s >= 60: return "D"
        return "F"

    def _page_grade(self, score: float) -> str:
        """Letter grade for a single page score."""
        if score >= 95: return "A+"
        if score >= 90: return "A"
        if score >= 85: return "B+"
        if score >= 80: return "B"
        if score >= 70: return "C"
        if score >= 60: return "D"
        return "F"

    def page_summary_table(self) -> str:
        """Formatted page-by-page accuracy table."""
        if not self.page_summaries:
            return "  (No page-by-page data available)"

        lines = []
        # Header
        lines.append(f"  {'Page':>4}  {'Tags':>6}  {'Content':>7}  {'Score':>6}  {'Grade':>5}  {'Issues':>6}  Notes")
        lines.append(f"  {'─'*4}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*40}")

        for ps in sorted(self.page_summaries, key=lambda p: p.get("page", 0)):
            pg       = ps.get("page", "?")
            tag_pct  = ps.get("tag_accuracy_pct", 0.0)
            cont_pct = ps.get("content_accuracy_pct", 0.0)
            score    = ps.get("page_score_pct", 0.0)
            issues   = ps.get("issues_on_page", 0)
            notes    = ps.get("page_notes", "")
            grade    = self._page_grade(score)

            # Highlight problem pages
            flag = "⚠️" if score < 95 else "✅"
            lines.append(
                f"  {pg:>4}  {tag_pct:5.1f}%  {cont_pct:6.1f}%  {score:5.1f}%  {grade:>4}  {issues:>5}   {flag} {notes[:60]}"
            )

        # Summary row
        if len(self.page_summaries) > 1:
            avg_tag  = sum(p.get("tag_accuracy_pct", 0) for p in self.page_summaries) / len(self.page_summaries)
            avg_cont = sum(p.get("content_accuracy_pct", 0) for p in self.page_summaries) / len(self.page_summaries)
            avg_score= sum(p.get("page_score_pct", 0) for p in self.page_summaries) / len(self.page_summaries)
            total_iss= sum(p.get("issues_on_page", 0) for p in self.page_summaries)
            min_score= min(p.get("page_score_pct", 0) for p in self.page_summaries)
            max_score= max(p.get("page_score_pct", 0) for p in self.page_summaries)
            perfect  = sum(1 for p in self.page_summaries if p.get("page_score_pct", 0) >= 100)
            lines.append(f"  {'─'*4}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*40}")
            lines.append(
                f"  {'AVG':>4}  {avg_tag:5.1f}%  {avg_cont:6.1f}%  {avg_score:5.1f}%  {self._page_grade(avg_score):>4}  {total_iss:>5}   "
                f"Range: {min_score:.0f}%-{max_score:.0f}% | {perfect}/{len(self.page_summaries)} pages perfect"
            )

        return "\n".join(lines)

    def summary(self) -> str:
        lines = [
            "=" * 70,
            "  VALIDATION REPORT — LLM-as-Judge",
            "=" * 70,
            f"  State:          {self.state_code}",
            f"  Judge Model:    {self.judge_model}",
            f"  PDF Pages:      {self.pdf_pages}",
            f"  Confidence:     {self.confidence}",
            "",
            "  ── OVERALL SCORES ──────────────────────────────",
            f"  Tag Accuracy:       {self.tag_accuracy_pct:6.1f}%",
            f"  Content Accuracy:   {self.content_accuracy_pct:6.1f}%",
            f"  Overall Score:      {self.overall_score_pct:6.1f}%   Grade: {self.grade()}",
        ]

        if self.golden_f1 is not None:
            lines += [
                "",
                "  ── GOLDEN REFERENCE COMPARISON ─────────────────",
                f"  Precision:   {self.golden_precision:.1%}",
                f"  Recall:      {self.golden_recall:.1%}",
                f"  F1 Score:    {self.golden_f1:.1%}",
            ]

        # Page-by-page breakdown
        lines += [
            "",
            "  ── PAGE-BY-PAGE ACCURACY ───────────────────────",
            self.page_summary_table(),
        ]

        lines += [
            "",
            "  ── STRUCTURAL STATS ────────────────────────────",
            f"  Addition [A>] tags:   {self.structural_stats.get('addition_tags', '?')}",
            f"  Deletion [D>] tags:   {self.structural_stats.get('deletion_tags', '?')}",
            f"  Mismatched openers:   {'YES ⚠️' if self.structural_stats.get('mismatched_additions') or self.structural_stats.get('mismatched_deletions') else 'None ✅'}",
            f"  Nested tags:          {self.structural_stats.get('nested_tags', 0)}",
            f"  Empty tags:           {self.structural_stats.get('empty_addition_tags', 0) + self.structural_stats.get('empty_deletion_tags', 0)}",
            f"  Word count:           {self.structural_stats.get('word_count', '?')}",
            "",
            "  ── ISSUES FOUND ────────────────────────────────",
            f"  Missed additions:     {len(self.missed_additions)}",
            f"  Missed deletions:     {len(self.missed_deletions)}",
            f"  False additions:      {len(self.false_additions)}",
            f"  False deletions:      {len(self.false_deletions)}",
            f"  Dropped text:         {len(self.dropped_text)}",
            f"  Misplaced tags:       {len(self.misplaced_tags)}",
            f"  Split/merge issues:   {len(self.split_merge_issues)}",
            f"  Layout issues:        {len(self.layout_issues)}",
            f"  TOTAL ISSUES:         {self.total_issues()}",
        ]

        if self.total_issues() == 0:
            lines.append("\n  ✅ No issues detected — conversion looks correct!")
        else:
            lines.append(f"\n  ⚠️  {self.total_issues()} issue(s) require attention.")

        lines.append("=" * 70)
        return "\n".join(lines)

    def detailed_report(self) -> str:
        """Full detailed report with every issue listed."""
        sections = [self.summary(), ""]

        if self.reasoning:
            sections += [
                "── JUDGE REASONING ───────────────────────────────",
                self.reasoning, ""
            ]

        def _issue_section(title: str, items: List[Dict], icon: str):
            if not items:
                return []
            lines = [f"── {title} ({len(items)}) ─────────────────────────"]
            for i, item in enumerate(items, 1):
                pg = f"  [page {item.get('page', '?')}]" if item.get("page") else ""
                text = item.get("text") or item.get("description", "")
                lines.append(f"  {icon} {i}. \"{text}\"{pg}")
                if item.get("expected"):
                    lines.append(f"       Expected: {item['expected']}")
                if item.get("actual"):
                    lines.append(f"       Actual:   {item['actual']}")
                if item.get("expected_split"):
                    lines.append(f"       Should be {item['expected_split']} separate tags")
            lines.append("")
            return lines

        sections += _issue_section("MISSED ADDITIONS (formatted in PDF, no [A>] tag)",
                                   self.missed_additions, "✗")
        sections += _issue_section("MISSED DELETIONS (formatted in PDF, no [D>] tag)",
                                   self.missed_deletions, "✗")
        sections += _issue_section("FALSE ADDITIONS (tagged [A>] but NOT formatted in PDF)",
                                   self.false_additions, "+")
        sections += _issue_section("FALSE DELETIONS (tagged [D>] but NOT formatted in PDF)",
                                   self.false_deletions, "+")
        sections += _issue_section("DROPPED TEXT (words in PDF missing from output)",
                                   self.dropped_text, "⊘")
        sections += _issue_section("MISPLACED TAGS (right text, wrong boundaries)",
                                   self.misplaced_tags, "↔")
        sections += _issue_section("SPLIT/MERGE ISSUES (tag grouping problems)",
                                   self.split_merge_issues, "↕")
        sections += _issue_section("LAYOUT ISSUES (line breaks, spacing, structure)",
                                   self.layout_issues, "⇄")

        return "\n".join(sections)


# ---------------------------------------------------------------------------
# Excel Report Generation
# ---------------------------------------------------------------------------
def generate_validation_excel(result: "ValidationResult", output_path: str,
                               state_config: Optional[Dict] = None) -> str:
    """
    Generate a formatted Excel report from a ValidationResult.

    Sheets:
      1. Issues           — Every issue found, colour-coded by severity
      2. Page Summary     — Per-page accuracy scores
      3. Overall Summary  — Scores, grade, structural stats

    Returns the path to the saved Excel file.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        log.error("openpyxl not installed. Run: pip install openpyxl")
        return ""

    wb = Workbook()

    # ── Colour palette ─────────────────────────────────────────────
    RED_FILL      = PatternFill("solid", fgColor="FFC7CE")   # high severity
    ORANGE_FILL   = PatternFill("solid", fgColor="FFE0B2")   # medium severity
    YELLOW_FILL   = PatternFill("solid", fgColor="FFF9C4")   # low severity
    GREEN_FILL    = PatternFill("solid", fgColor="C8E6C9")   # good / match
    HEADER_FILL   = PatternFill("solid", fgColor="1565C0")
    HEADER_FONT   = Font(bold=True, color="FFFFFF", size=11)
    BOLD_FONT     = Font(bold=True, size=11)
    WRAP_ALIGN    = Alignment(wrap_text=True, vertical="top")
    THIN_BORDER   = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"),  bottom=Side(style="thin"),
    )

    def _add_header_row(ws, headers, widths=None):
        for col, hdr in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=hdr)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = THIN_BORDER
        if widths:
            for col, w in enumerate(widths, 1):
                ws.column_dimensions[chr(64 + col) if col <= 26
                    else chr(64 + (col - 1) // 26) + chr(65 + (col - 1) % 26)].width = w
        ws.auto_filter.ref = f"A1:{chr(64 + len(headers))}1"
        ws.freeze_panes = "A2"

    # ── Derive state formatting rules ──────────────────────────────
    add_note = ""
    del_note = ""
    if state_config:
        add_cfg = state_config.get("add_markup", {})
        del_cfg = state_config.get("delete_markup", {})
        add_parts = []
        if add_cfg.get("underline"):  add_parts.append("underline")
        if add_cfg.get("bold"):       add_parts.append("bold")
        if add_cfg.get("italic"):     add_parts.append("italic")
        if add_cfg.get("all_caps"):   add_parts.append("ALL CAPS (text-based)")
        if add_cfg.get("brackets"):   add_parts.append("brackets (text-based)")
        add_note = ", ".join(add_parts) if add_parts else "unknown"
        del_parts = []
        if del_cfg.get("strike_out"): del_parts.append("strikethrough")
        if del_cfg.get("brackets"):   del_parts.append("brackets (text-based)")
        if del_cfg.get("italic"):     del_parts.append("italic")
        del_note = ", ".join(del_parts) if del_parts else "unknown"

    # ── Sheet 1: Validation Results (per-segment inventory) ──────
    ws_seg = wb.active
    ws_seg.title = "Validation Results"

    seg_headers = [
        "Page", "Bill Line", "Text", "Format Type", "Expected Tag",
        "Tag Found?", "Text Match", "Missing Chars", "Severity",
        "Notes", "Suggested Fix", "Source", "Actual Tag Content",
    ]
    _add_header_row(ws_seg, seg_headers,
                    [8, 11, 55, 16, 16, 13, 13, 30, 12, 45, 45, 10, 55])

    # Severity → fill colour mapping
    sev_fill = {
        "critical": RED_FILL,
        "high":     RED_FILL,
        "medium":   ORANGE_FILL,
        "low":      YELLOW_FILL,
        "ok":       GREEN_FILL,
    }

    row = 2
    for seg in result.formatted_segments:
        page          = seg.get("page", "")
        bill_line     = seg.get("bill_line", "")
        text          = seg.get("text", "")
        format_type   = seg.get("format_type", "")
        expected_tag  = seg.get("expected_tag", "")
        tag_found     = seg.get("tag_found", "")
        text_match    = seg.get("text_match", "")
        missing_chars = seg.get("missing_chars", "")
        severity      = seg.get("severity", "OK")
        notes         = seg.get("notes", "")
        suggested_fix = seg.get("suggested_fix", "")
        source        = seg.get("_source", "gpt")
        matched_tag   = seg.get("_matched_tag", "")

        vals = [page, bill_line, text[:300], format_type, expected_tag,
                tag_found, text_match, missing_chars[:200],
                severity, notes[:200], suggested_fix[:200],
                source, matched_tag[:300]]
        for col, v in enumerate(vals, 1):
            cell = ws_seg.cell(row=row, column=col, value=v)
            cell.alignment = WRAP_ALIGN
            cell.border = THIN_BORDER

        # Colour the severity cell (column 9)
        sev_key = str(severity).lower().strip()
        fill = sev_fill.get(sev_key, GREEN_FILL)
        sev_cell = ws_seg.cell(row=row, column=9)
        sev_cell.fill = fill
        sev_cell.font = BOLD_FONT

        # Colour Tag Found? (column 6): green=Yes, red=No, orange=Partial
        tf_val = str(tag_found).strip().lower()
        tf_cell = ws_seg.cell(row=row, column=6)
        if tf_val == "yes":
            tf_cell.fill = GREEN_FILL
        elif tf_val == "no":
            tf_cell.fill = RED_FILL
            tf_cell.font = BOLD_FONT
        elif tf_val == "partial":
            tf_cell.fill = ORANGE_FILL
            tf_cell.font = BOLD_FONT

        # Colour Text Match (column 7): green=Yes, red=No, orange=Partial
        tm_val = str(text_match).strip().lower()
        tm_cell = ws_seg.cell(row=row, column=7)
        if tm_val == "yes":
            tm_cell.fill = GREEN_FILL
        elif tm_val == "no":
            tm_cell.fill = RED_FILL
            tm_cell.font = BOLD_FONT
        elif tm_val == "partial":
            tm_cell.fill = ORANGE_FILL
            tm_cell.font = BOLD_FONT

        row += 1

    if row == 2:
        # No segments found — fallback message
        cell = ws_seg.cell(row=2, column=1, value="No formatted segments returned by judge")
        cell.fill = YELLOW_FILL
        cell.font = BOLD_FONT
        ws_seg.merge_cells("A2:M2")

    # ── Sheet 2: Page Summary ──────────────────────────────────────
    ws_pages = wb.create_sheet("Page Summary")
    page_headers = [
        "Page", "Tag Accuracy %", "Content Accuracy %",
        "Page Score %", "Grade", "Issues", "Notes",
    ]
    _add_header_row(ws_pages, page_headers, [8, 18, 20, 16, 10, 10, 55])

    for ps_row, ps in enumerate(
        sorted(result.page_summaries, key=lambda p: p.get("page", 0)), start=2
    ):
        tag_pct  = ps.get("tag_accuracy_pct", 0.0)
        cont_pct = ps.get("content_accuracy_pct", 0.0)
        score    = ps.get("page_score_pct", 0.0)
        issues   = ps.get("issues_on_page", 0)
        notes    = ps.get("page_notes", "")
        grade    = result._page_grade(score)

        vals = [ps.get("page", ""), tag_pct, cont_pct, score, grade, issues, notes]
        for col, v in enumerate(vals, 1):
            cell = ws_pages.cell(row=ps_row, column=col, value=v)
            cell.alignment = WRAP_ALIGN
            cell.border = THIN_BORDER
            if col == 4:  # score column
                if score >= 95:
                    cell.fill = GREEN_FILL
                elif score >= 80:
                    cell.fill = YELLOW_FILL
                else:
                    cell.fill = RED_FILL

    # ── Sheet 3: Overall Summary ───────────────────────────────────
    ws_summary = wb.create_sheet("Overall Summary")
    _add_header_row(ws_summary, ["Metric", "Value"], [35, 30])

    summary_rows = [
        ("State",               result.state_code),
        ("Judge Model",         result.judge_model),
        ("PDF Pages",           result.pdf_pages),
        ("Confidence",          result.confidence),
        ("",                    ""),
        ("Tag Accuracy %",      f"{result.tag_accuracy_pct:.1f}%"),
        ("Content Accuracy %",  f"{result.content_accuracy_pct:.1f}%"),
        ("Overall Score %",     f"{result.overall_score_pct:.1f}%"),
        ("Grade",               result.grade()),
        ("",                    ""),
        ("Missed Additions",    len(result.missed_additions)),
        ("Missed Deletions",    len(result.missed_deletions)),
        ("False Additions",     len(result.false_additions)),
        ("False Deletions",     len(result.false_deletions)),
        ("Dropped Text",        len(result.dropped_text)),
        ("Misplaced Tags",      len(result.misplaced_tags)),
        ("Split/Merge Issues",  len(result.split_merge_issues)),
        ("Layout Issues",       len(result.layout_issues)),
        ("Total Issues",        result.total_issues()),
        ("",                    ""),
        ("Addition Tags [A>]",  result.structural_stats.get("addition_tags", "?")),
        ("Deletion Tags [D>]",  result.structural_stats.get("deletion_tags", "?")),
        ("Word Count",          result.structural_stats.get("word_count", "?")),
    ]

    # Add state formatting rules
    if add_note or del_note:
        summary_rows.append(("", ""))
        summary_rows.append(("[A>] Formatting", add_note))
        summary_rows.append(("[D>] Formatting", del_note))

    for r, (metric, value) in enumerate(summary_rows, 2):
        m_cell = ws_summary.cell(row=r, column=1, value=metric)
        v_cell = ws_summary.cell(row=r, column=2, value=value)
        m_cell.font = BOLD_FONT if metric else Font()
        m_cell.border = THIN_BORDER
        v_cell.border = THIN_BORDER

        # Colour the grade
        if metric == "Grade":
            grade = result.grade()
            if grade in ("A+", "A"):
                v_cell.fill = GREEN_FILL
            elif grade in ("B+", "B"):
                v_cell.fill = YELLOW_FILL
            else:
                v_cell.fill = RED_FILL
            v_cell.font = BOLD_FONT

    # Save
    try:
        wb.save(output_path)
        log.info(f"📊 Validation Excel saved to: {output_path}")
        return output_path
    except Exception as e:
        log.error(f"Failed to save Excel: {e}")
        return ""


# ---------------------------------------------------------------------------
# PyMuPDF Ground Truth → Checklist for GPT
# ---------------------------------------------------------------------------
def _map_format_to_tag(fmt_type: str, state_config: Optional[Dict]) -> str:
    """Map a PyMuPDF format type to the expected tag based on state rules."""
    if not state_config:
        # Defaults: underline→addition, strikethrough→deletion
        if fmt_type in ("underline", "bold", "italic"):
            return "[A>...<A]"
        if fmt_type in ("strikethrough",):
            return "[D>...<D]"
        return "[A>...<A]"

    add_cfg = state_config.get("add_markup", {})
    del_cfg = state_config.get("delete_markup", {})

    # Check if this format indicates addition
    add_map = {
        "underline": add_cfg.get("underline", False),
        "bold":      add_cfg.get("bold", False),
        "italic":    add_cfg.get("italic", False),
    }
    if add_map.get(fmt_type, False):
        return "[A>...<A]"

    # Check if this format indicates deletion
    del_map = {
        "strikethrough": del_cfg.get("strike_out", False),
        "italic":        del_cfg.get("italic", False),
    }
    if del_map.get(fmt_type, False):
        return "[D>...<D]"

    # Fallback: underline=addition, strikethrough=deletion
    if fmt_type == "underline":
        return "[A>...<A]"
    if fmt_type == "strikethrough":
        return "[D>...<D]"
    return "[A>...<A]"


def _build_ground_truth_checklist(
    pymupdf_report: Dict,
    state_config: Optional[Dict] = None,
) -> Tuple[str, int]:
    """
    Convert PyMuPDF formatted_spans into a numbered checklist for the GPT prompt.

    Merges consecutive spans split by PDF line breaks, then filters out
    header/footer spans (bill_line is null).

    Returns:
        (checklist_text, n_spans) — the formatted string and count of content spans
    """
    spans = pymupdf_report.get("formatted_spans", [])
    if not spans:
        return "", 0

    # Merge consecutive spans and filter content only
    merged = _merge_consecutive_spans(spans)
    content_spans = [s for s in merged if s.get("bill_line") is not None]
    if not content_spans:
        return "", 0

    lines = []
    for i, span in enumerate(content_spans, 1):
        page = span.get("page", "?")
        bill_line = span.get("bill_line", "")
        text = span.get("text", "").strip()
        fmt_types = span.get("format_types", [])
        fmt_str = ", ".join(fmt_types) if fmt_types else "unknown"
        expected_tag = _map_format_to_tag(fmt_types[0] if fmt_types else "underline", state_config)
        context = span.get("context", "")

        line = f"  Span {i}: Page {page}, Line {bill_line}, \"{text}\" ({fmt_str}) → Expected: {expected_tag}"
        if context:
            line += f"\n          Context: {context}"
        lines.append(line)

    checklist = "\n".join(lines)
    return checklist, len(content_spans)


# ---------------------------------------------------------------------------
# Merge consecutive PyMuPDF spans that wrap across PDF lines
# ---------------------------------------------------------------------------
def _spans_are_continuous(current_span: Dict, next_span: Dict) -> bool:
    """
    Check if two adjacent PyMuPDF spans are really one continuous formatted
    run that was split by a PDF line break.

    Heuristic: if the current span's context ENDS with '<<<' (formatted text
    at the end of the PDF line) and the next span's context STARTS with '>>>'
    (formatted text at the start of the next PDF line), they are a
    continuation of the same underlined/formatted phrase.

    If there is normal text after '<<<' in the current context, or before
    '>>>' in the next context, then unformatted text separates them and they
    are independent formatted segments.
    """
    c_ctx = current_span.get("context", "")
    n_ctx = next_span.get("context", "")

    if not c_ctx or not n_ctx:
        return False

    # Current span ends at end of line?  (context ends with <<<, maybe whitespace)
    c_after_marker = c_ctx.rsplit("<<<", 1)[-1].strip() if "<<<" in c_ctx else "has_text"
    current_ends_at_edge = (c_after_marker == "")

    # Next span starts at start of line?  (context starts with >>>, maybe whitespace)
    n_before_marker = n_ctx.split(">>>", 1)[0].strip() if ">>>" in n_ctx else "has_text"
    next_starts_at_edge = (n_before_marker == "")

    return current_ends_at_edge and next_starts_at_edge


def _merge_consecutive_spans(spans: List[Dict]) -> List[Dict]:
    """
    Merge adjacent PyMuPDF spans that are part of the same logical formatted
    run but split across PDF line breaks.

    Merge criteria — two consecutive spans are merged when ALL of:
      1. Same page
      2. Same format_types
      3. bill_line numbers differ by 0 or 1 (adjacent lines)
      4. Both have bill_line != None (content, not headers)
      5. Context shows they are truly continuous (no normal text between them)

    Returns a new list of merged span dicts.
    """
    if not spans:
        return []

    # Only work with content spans (bill_line not None)
    content = [s for s in spans if s.get("bill_line") is not None]
    headers = [s for s in spans if s.get("bill_line") is None]

    if not content:
        return headers

    # Sort by page, then by bill_line, then by bbox-y (top) for same-line spans
    content.sort(key=lambda s: (
        s.get("page", 0),
        int(s.get("bill_line", 0)),
        s.get("bbox", [0, 0, 0, 0])[0],  # left-x as tiebreaker
    ))

    merged: List[Dict] = []
    current = dict(content[0])  # deep-ish copy
    current["text"] = current.get("text", "").strip()
    current["_original_spans"] = [content[0].get("text", "").strip()]

    for span in content[1:]:
        s_page = span.get("page", 0)
        s_line = int(span.get("bill_line", 0))
        s_fmt  = span.get("format_types", [])
        s_text = span.get("text", "").strip()

        c_page = current.get("page", 0)
        c_line = int(current.get("bill_line", 0))
        c_fmt  = current.get("format_types", [])

        # Check merge criteria
        same_page = (s_page == c_page)
        same_fmt  = (s_fmt == c_fmt)
        adjacent  = (0 <= s_line - c_line <= 1)
        continuous = _spans_are_continuous(current, span)

        if same_page and same_fmt and adjacent and continuous:
            # Merge: concatenate text with a space
            current["text"] = current["text"] + " " + s_text
            current["bill_line"] = s_line  # update to latest line
            current["_original_spans"].append(s_text)
            # Update context to the next span's (so further merges check the right edge)
            current["context"] = span.get("context", current.get("context", ""))
        else:
            # Finalize current, start new group
            merged.append(current)
            current = dict(span)
            current["text"] = s_text
            current["_original_spans"] = [s_text]

    merged.append(current)

    # Return headers + merged content (headers first so they get filtered later)
    return headers + merged


# ---------------------------------------------------------------------------
# Programmatic (Python) tag matching — deterministic, no LLM needed
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r'\[([AD])>(.*?)<\1\]', re.DOTALL)


def _extract_tags_from_output(converted_text: str) -> List[Dict]:
    """
    Extract every [A>...<A] and [D>...<D] tag from the converted output.

    Returns a list of dicts:
      { "tag_type": "A"|"D", "content": "text inside tag",
        "start": int, "end": int }
    sorted by position in the text.
    """
    tags = []
    for m in _TAG_RE.finditer(converted_text):
        tags.append({
            "tag_type": m.group(1),           # "A" or "D"
            "content":  m.group(2),            # text inside [A>...<A]
            "start":    m.start(),
            "end":      m.end(),
        })
    return tags


def _norm(text: str) -> str:
    """Collapse whitespace for comparison."""
    return " ".join(text.split()).strip()


def _programmatic_match_spans(
    pymupdf_report: Dict,
    converted_text: str,
    state_config: Optional[Dict] = None,
) -> List[Dict]:
    """
    For each PyMuPDF ground-truth span, search the converted output for the
    matching tag and do *exact* string comparison.

    Returns a formatted_segments list with deterministic tag_found / text_match /
    missing_chars / severity values — no LLM involved.
    """
    spans = pymupdf_report.get("formatted_spans", [])

    # ── Merge consecutive spans that were split by PDF line breaks ──
    merged_spans = _merge_consecutive_spans(spans)
    content_spans = [s for s in merged_spans if s.get("bill_line") is not None]
    if not content_spans:
        return []

    log.info(f"🔗 Merged PyMuPDF spans: {sum(1 for s in spans if s.get('bill_line') is not None)} "
             f"raw → {len(content_spans)} merged")

    # Pre-extract all tags from the converted output
    output_tags = _extract_tags_from_output(converted_text)

    segments: List[Dict] = []

    for span in content_spans:
        page = span.get("page", "")
        bill_line = span.get("bill_line", "")
        gt_text = span.get("text", "").strip()          # ground truth text
        fmt_types = span.get("format_types", [])
        fmt_str = fmt_types[0] if fmt_types else "underline"
        expected_tag = _map_format_to_tag(fmt_str, state_config)
        expected_type = "A" if "A>" in expected_tag else "D"

        gt_norm = _norm(gt_text)

        # ── Try to find this span in the converted output ────────
        tag_found = "No"
        text_match = "N/A"
        missing_chars = ""
        extra_chars = ""
        severity = "Critical"
        notes = "Tag not found for formatted text"
        suggested_fix = ""
        matched_content = ""

        # Strategy 1: EXACT — ground truth text appears inside a tag of the right type
        best_match = None
        best_score = 0.0

        for t in output_tags:
            if t["tag_type"] != expected_type:
                continue
            tag_content = t["content"]
            tag_norm = _norm(tag_content)

            # Exact match
            if gt_norm == tag_norm:
                best_match = t
                best_score = 1.0
                break

            # Ground truth is a substring of tag content (tag covers MORE than expected)
            if gt_norm in tag_norm:
                score = len(gt_norm) / max(len(tag_norm), 1)
                if score > best_score:
                    best_match = t
                    best_score = score

            # Tag content is a substring of ground truth (tag covers LESS than expected)
            if tag_norm in gt_norm and len(tag_norm) >= 2:
                score = len(tag_norm) / max(len(gt_norm), 1)
                if score > best_score:
                    best_match = t
                    best_score = score

        # Strategy 2: If no tag match, check if the ground truth text appears
        # ANYWHERE in the output (just not tagged)
        gt_in_output = gt_norm in _norm(converted_text) if not best_match else True

        if best_match:
            matched_content = best_match["content"].strip()
            matched_norm = _norm(matched_content)

            if best_score == 1.0:
                # Perfect exact match
                tag_found = "Yes"
                text_match = "Yes"
                severity = "OK"
                notes = "Correct"
                suggested_fix = ""
            else:
                tag_found = "Partial" if best_score >= 0.3 else "No"

                # Figure out what's missing or extra
                if gt_norm in matched_norm:
                    # Tag has MORE text than ground truth
                    extra_part = matched_norm.replace(gt_norm, "").strip()
                    text_match = "Partial"
                    extra_chars = extra_part
                    missing_chars = ""
                    severity = "Medium"
                    notes = f"Tag includes extra text: \"{extra_part}\""
                    suggested_fix = f"[{expected_type}>{gt_text}<{expected_type}]"
                elif matched_norm in gt_norm:
                    # Tag has LESS text than ground truth
                    missing_part = gt_norm.replace(matched_norm, "").strip()
                    text_match = "Partial"
                    missing_chars = missing_part
                    severity = "Medium"
                    notes = f"Tag missing: \"{missing_part}\""
                    suggested_fix = f"[{expected_type}>{gt_text}<{expected_type}]"
                else:
                    # Some overlap but not a clean subset
                    text_match = "Partial"
                    # Character-level diff
                    gt_chars = set(gt_norm)
                    matched_chars = set(matched_norm)
                    missing_set = gt_chars - matched_chars
                    extra_set = matched_chars - gt_chars
                    if missing_set:
                        missing_chars = "".join(sorted(missing_set))
                    if extra_set:
                        extra_chars = "".join(sorted(extra_set))
                    severity = "Medium"
                    notes = f"Text differs — expected: \"{gt_text}\", found: \"{matched_content}\""
                    suggested_fix = f"[{expected_type}>{gt_text}<{expected_type}]"
        else:
            # No tag found at all
            if gt_in_output:
                tag_found = "No"
                text_match = "N/A"
                severity = "Critical"
                notes = f"Text \"{gt_text}\" exists in output but is NOT tagged"
                suggested_fix = f"[{expected_type}>{gt_text}<{expected_type}]"
            else:
                tag_found = "No"
                text_match = "N/A"
                severity = "Critical"
                notes = f"Text \"{gt_text}\" not found in output at all"
                suggested_fix = f"[{expected_type}>{gt_text}<{expected_type}]"

        segments.append({
            "page":          page,
            "bill_line":     str(bill_line),
            "text":          gt_text,
            "format_type":   fmt_str,
            "expected_tag":  expected_tag,
            "tag_found":     tag_found,
            "text_match":    text_match,
            "missing_chars": missing_chars,
            "severity":      severity,
            "notes":         notes,
            "suggested_fix": suggested_fix,
            "_source":       "python",       # track origin
            "_matched_tag":  matched_content, # what was actually in the tag
        })

    return segments


# ---------------------------------------------------------------------------
# LLM Judge Prompt
# ---------------------------------------------------------------------------
_JUDGE_PROMPT = """You are a STRICT quality auditor for legislative bill PDF-to-text conversion.

A converter (Claude Opus 4.6) was given a PDF of a {state_code} legislative bill and asked to
produce a tagged plain-text version.  Your job is to JUDGE the quality of that conversion
by comparing the converted output against the original PDF page images.

{state_rules}

TAG FORMAT:
  [A>added text<A]  — marks text that is visually formatted as an ADDITION in the PDF
  [D>deleted text<D] — marks text that is visually formatted as a DELETION in the PDF
  All other text should appear as plain text with NO tags.

I am showing you {n_pages} page image(s) of the original PDF.{highlighted_section}
{golden_section}

IMAGE TYPES YOU WILL SEE:
  1. ORIGINAL PAGE IMAGES — Raw PDF pages rendered at high DPI. Use these to read all text
     and verify content completeness.
  2. HIGHLIGHTED PAGE IMAGES (if provided) — Same pages with coloured overlays showing
     formatting detected by a PDF parser (PyMuPDF). The colour code is:
       🟢 Green  = UNDERLINED text
       🔴 Red    = STRIKETHROUGH text
       🔵 Blue   = ITALIC text
       🟠 Orange = BOLD text
     NOTE: These highlights only show VISUAL formatting. TEXT-BASED formatting such as
     ALL CAPS or [BRACKETS] will NOT have any colour overlay. You must detect those
     by reading the actual text content.

TEXT-BASED FORMATTING — CRITICAL:
  Some states use text-based conventions to indicate additions or deletions:
  - ALL CAPS:  Words typed in UPPERCASE indicate added language (e.g., Colorado).
    These look like regular text in images — no colour overlay. Check by reading the text.
  - BRACKETS:  Text enclosed in [square brackets] indicates deleted language (e.g., Connecticut).
    These look like regular text in images — no colour overlay. Check by reading the text.
  The state rules above tell you exactly which formatting types apply for {state_code}.
  For text-based formatting, compare the converted output against the raw PDF text to verify
  that every ALL CAPS word or [bracketed passage] has the correct tag.

YOUR TASK — examine EVERY page image INDIVIDUALLY and check the converted output for ALL of
these issues:

1. MISSED ADDITIONS — text is visually formatted as addition in PDF but has NO [A>...<A] tag.
2. MISSED DELETIONS — text has deletion formatting in PDF but has NO [D>...<D] tag.
3. FALSE ADDITIONS — output has [A>text<A] but that text is NOT addition-formatted in the PDF.
4. FALSE DELETIONS — output has [D>text<D] but that text is NOT deletion-formatted in the PDF.
5. DROPPED TEXT — words, phrases, or sentences visible in the PDF that are COMPLETELY MISSING
   from the output (not just untagged — entirely absent).
6. MISPLACED TAGS — the correct text is tagged but the tag BOUNDARIES are wrong
   (starts too early/late, ends too early/late, includes extra words or misses some).
7. SPLIT/MERGE ISSUES — content is tagged but merged into one tag when it should be separate,
   or split into multiple tags when it should be one continuous tag.
8. LAYOUT ISSUES — wrong line breaks, missing/extra blank lines, text on wrong line,
   structural spacing problems.  These are NOT tagging errors.

PUNCTUATION ATTENTION — CRITICAL:
  Pay VERY close attention to individual punctuation characters: commas (,), periods (.),
  semicolons (;), colons (:), and other special characters. These are frequently formatted
  (underlined, italic, strikethrough) independently in legislative PDFs but are easy to miss.
  For EACH punctuation mark near a tag boundary or between tagged sections, zoom in mentally
  and check: is this character underlined/italic/strikethrough in the PDF? If yes, it MUST
  have its own tag or be included in an adjacent tag. A missing [A>,<A] or [D>;<D] is just
  as important as a missing tagged word. Report each missed punctuation mark separately.

PAGE-BY-PAGE SCORING — MANDATORY:
  You MUST produce a "page_summaries" array in the JSON output — one entry per PDF page.
  For EACH page, independently assess:
  - tag_accuracy_pct:     What % of tags on THIS page are correct? (0-100)
  - content_accuracy_pct: What % of THIS page's text content is present in the output? (0-100)
  - page_score_pct:       Combined score for THIS page: tags(60%) + content(40%)
  - issues_on_page:       Total number of issues found on THIS page
  - page_notes:           One-sentence summary of what went right or wrong on this page

  The per-page scores let us quickly pinpoint which pages cause accuracy drops.

SEGMENT-BY-SEGMENT INVENTORY — MANDATORY:
  You MUST produce a "formatted_segments" array in the JSON output.
  {ground_truth_instruction}

  Fields for each segment:
  - page:          PDF page number (1-based)
  - bill_line:     The bill line number printed at the left margin nearest to this text
                   (e.g. "5", "12"). Use "" if no line number is visible.
  - text:          The exact formatted text (the words that are underlined/bracketed/etc.)
  - format_type:   "underline", "strikethrough", "italic", "bold", "ALL CAPS", "brackets"
  - expected_tag:  "[A>...<A]" or "[D>...<D]" based on the state rules above
  - tag_found:     "Yes" if the expected tag wraps this text in the output,
                   "No" if the tag is completely missing,
                   "Partial" if a tag exists but does not cover the full text
  - text_match:    "Yes" if the text inside the tag exactly matches the PDF,
                   "No" if different, "Partial" if mostly matches but some chars differ,
                   "N/A" if the tag was not found at all
  - missing_chars: Specific characters or words that should be inside the tag but are missing.
                   Use "" if nothing is missing.
  - severity:      "Critical" if tag completely missing for formatted text,
                   "High" if significant text mismatch or wrong tag type,
                   "Medium" if minor boundary issue (e.g. missed comma/semicolon),
                   "Low" if cosmetic or spacing only,
                   "OK" if no problem at all
  - notes:         Brief description of the issue, or "Correct" if no problem
  - suggested_fix: If there is an issue, show the corrected tag (e.g. "[A>word,<A]").
                   Leave "" if the segment is correct.

  IMPORTANT: List EVERY segment, not just the ones with problems.
  Segments that are correctly tagged should have tag_found="Yes", text_match="Yes",
  severity="OK", notes="Correct", suggested_fix="".

  POSITION VERIFICATION — CRITICAL:
  For EACH segment, verify not just that the tag EXISTS but that it is in the
  CORRECT POSITION relative to surrounding text. Look at the PDF image to see
  the actual text order. If a tag is placed before/after the wrong word, or if
  two adjacent tags are swapped, report it as severity="Medium" or "High" with
  notes explaining the position error and suggested_fix showing the correct placement.

OVERALL SCORING RULES — assign three accuracy percentages:
  - tag_accuracy_pct (0-100):     What % of tags are correctly placed across ALL pages?
                                  100 = every tag is perfect; 0 = all wrong
  - content_accuracy_pct (0-100): What % of PDF text content is present in the output?
                                  100 = nothing dropped; 0 = everything missing
  - overall_score_pct (0-100):    Combined quality score weighing tags (60%) + content (40%)

  STRICT SCORING CONSISTENCY:
  - If you report ZERO issues in a category, the score for that category MUST be 100.0%.
    Do NOT hedge by deducting points "just in case" when you cannot identify a specific problem.
  - If missed_additions, missed_deletions, false_additions, and false_deletions are ALL empty,
    then tag_accuracy_pct MUST be 100.0.
  - If dropped_text is empty, then content_accuracy_pct MUST be 100.0.
  - Only deduct points when you have listed the SPECIFIC issue causing the deduction.
  - Minor whitespace or blank-line differences are NOT content issues — do not deduct for them.

CRITICAL RULES:
  - Skip page numbers, line numbers at left margin, and repeating page headers/footers.
  - Do NOT report the same text in multiple categories.
  - Give page number (1-based) for each finding.
  - Quote EXACT text from the output or PDF.
  - Be strict but fair — minor whitespace differences are not issues.

WORD-BY-WORD VERIFICATION — CRITICAL:
  For EACH page, read every word in the PDF image and confirm it exists in the converted output.
  Pay special attention to:
  - Small connecting words: "a", "the", "or", "and", "of", "in", "to", "by", "for"
  - Words at the END of lines or at page breaks — these are most commonly dropped
  - Words near tag boundaries — when Claude adds [A> or [D> tags, it sometimes
    accidentally drops the word right before or after the tag
  - Subsection labels like (a), (b), (1), (2) — check each one is present
  If ANY word visible in the PDF is completely absent from the output, report it
  as DROPPED TEXT with the exact missing word(s) and page number.

STRUCTURAL PRE-ANALYSIS (regex-based, for your reference):
{structural_info}

CONVERTED OUTPUT TO VERIFY:
───────────────────────────────────────
{output_text}
───────────────────────────────────────

Respond with ONLY a valid JSON object (no markdown fences, no explanation outside JSON):
{{
  "reasoning": "Detailed page-by-page analysis of what you found",
  "tag_accuracy_pct": 92.5,
  "content_accuracy_pct": 98.0,
  "overall_score_pct": 94.7,
  "confidence": "high",
  "page_summaries": [
    {{
      "page": 1,
      "tag_accuracy_pct": 100.0,
      "content_accuracy_pct": 100.0,
      "page_score_pct": 100.0,
      "issues_on_page": 0,
      "page_notes": "All tags correct, no dropped text."
    }},
    {{
      "page": 2,
      "tag_accuracy_pct": 85.0,
      "content_accuracy_pct": 95.0,
      "page_score_pct": 89.0,
      "issues_on_page": 3,
      "page_notes": "Missed 2 underline tags in subsection (c); one word dropped at line end."
    }}
  ],
  "missed_additions": [
    {{"text": "exact untagged text", "page": 2}}
  ],
  "missed_deletions": [
    {{"text": "exact untagged text", "page": 3}}
  ],
  "false_additions": [
    {{"text": "text incorrectly in [A>]", "page": 1}}
  ],
  "false_deletions": [
    {{"text": "text incorrectly in [D>]", "page": 1}}
  ],
  "dropped_text": [
    {{"text": "words completely missing from output", "page": 4}}
  ],
  "misplaced_tags": [
    {{"text": "tagged text", "page": 2, "expected": "what it should be", "actual": "what it is"}}
  ],
  "split_merge_issues": [
    {{"text": "merged tag content", "page": 5, "expected_split": 3}}
  ],
  "layout_issues": [
    {{"description": "description of layout problem", "page": 1}}
  ],
  "formatted_segments": [
    {{
      "page": 1,
      "bill_line": "5",
      "text": "three utility commissioners",
      "format_type": "underline",
      "expected_tag": "[A>...<A]",
      "tag_found": "Yes",
      "text_match": "Yes",
      "missing_chars": "",
      "severity": "OK",
      "notes": "Correct",
      "suggested_fix": ""
    }},
    {{
      "page": 2,
      "bill_line": "12",
      "text": "thereafter,",
      "format_type": "brackets",
      "expected_tag": "[D>...<D]",
      "tag_found": "Partial",
      "text_match": "Partial",
      "missing_chars": "comma after thereafter",
      "severity": "Medium",
      "notes": "Bracket deletion found but comma excluded from tag",
      "suggested_fix": "[D>thereafter,<D]"
    }}
  ]
}}

confidence: "high" = certain about findings | "medium" = some ambiguity | "low" = images unclear
"""


def _build_judge_prompt(
    state_code: str,
    output_text: str,
    n_pages: int,
    structural_info: str,
    golden_text: Optional[str] = None,
    state_config: Optional[Dict] = None,
    has_highlighted_images: bool = False,
    pymupdf_checklist: str = "",
    pymupdf_span_count: int = 0,
) -> str:
    state_rules = describe_state_rules(state_code, state_config)

    golden_section = ""
    if golden_text:
        golden_snippet = golden_text[:40000] if len(golden_text) > 40000 else golden_text
        golden_section = (
            f"\nGOLDEN REFERENCE (correct output for comparison):\n"
            f"───────────────────────────────────────\n"
            f"{golden_snippet}\n"
            f"───────────────────────────────────────\n"
            f"Use this golden reference to cross-check your visual findings.\n"
        )

    highlighted_section = ""
    if has_highlighted_images:
        highlighted_section = (
            f"\n  After the {n_pages} original page images, you will also see "
            f"{n_pages} HIGHLIGHTED page images with colour-coded formatting overlays."
        )

    # Build ground truth instruction based on whether PyMuPDF data is available
    if pymupdf_checklist:
        ground_truth_instruction = (
            f"A PDF parser (PyMuPDF) has detected {pymupdf_span_count} formatted text spans "
            f"with character-level precision.\n\n"
            f"  IMPORTANT: A Python program has ALREADY performed exact string matching for\n"
            f"  all {pymupdf_span_count} visual spans (tag_found, text_match, missing_chars).\n"
            f"  You do NOT need to check whether tags exist or whether text matches for these spans.\n"
            f"  Those results are handled programmatically with 100% accuracy.\n\n"
            f"  YOUR JOB for the visual spans below is ONLY:\n"
            f"  1. POSITION VERIFICATION — look at each span in the PDF image and confirm the tag\n"
            f"     is in the CORRECT POSITION relative to surrounding text (not swapped, not shifted).\n"
            f"  2. If you find a POSITION issue, include that span in your formatted_segments with\n"
            f"     format_type matching the checklist, and notes describing the position problem.\n\n"
            f"  GROUND TRUTH CHECKLIST (visual formatting from PDF parser):\n"
            f"{pymupdf_checklist}\n\n"
            f"  YOUR MAIN JOB — TEXT-BASED FORMATTING:\n"
            f"  For text-based formatting (brackets, ALL CAPS) that the PDF parser cannot detect,\n"
            f"  you MUST identify these by reading the PDF text yourself.\n"
            f"  For EACH bracketed passage or ALL CAPS word, check the converted output and report:\n"
            f"  tag_found, text_match, missing_chars, severity, notes, suggested_fix.\n"
            f"  Include these as entries in the formatted_segments array with\n"
            f"  format_type = \"brackets\" or \"ALL CAPS\".\n\n"
            f"  ALSO check for: dropped text, false additions/deletions, layout issues."
        )
    else:
        ground_truth_instruction = (
            "No PDF parser ground truth is available.\n"
            "  You must identify EVERY piece of formatted text visible in the PDF images —\n"
            "  whether it is underlined, in [brackets], struck-through, italic, bold, or ALL CAPS.\n"
            "  Include segments that were correctly tagged AND segments that were missed."
        )

    output_snippet = output_text[:128000] if len(output_text) > 128000 else output_text
    if len(output_text) > 128000:
        log.warning(f"⚠️  Output text truncated from {len(output_text)} to 128000 chars for judge prompt")

    return _JUDGE_PROMPT.format(
        state_code=state_code,
        state_rules=state_rules,
        n_pages=n_pages,
        golden_section=golden_section,
        highlighted_section=highlighted_section,
        structural_info=structural_info,
        output_text=output_snippet,
        ground_truth_instruction=ground_truth_instruction,
    )


# ---------------------------------------------------------------------------
# JSON repair (from ace_visual_judge.py)
# ---------------------------------------------------------------------------
def _repair_truncated_json(raw: str) -> str:
    """Best-effort repair of JSON truncated by max_output_tokens."""
    raw = raw.strip()
    openers = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            openers.append(ch)
        elif ch in ("}", "]"):
            if openers:
                openers.pop()
    if in_string:
        raw += '"'
    raw = raw.rstrip().rstrip(",")
    for opener in reversed(openers):
        raw += "}" if opener == "{" else "]"
    return raw


def _strip_markdown_fences(raw: str) -> str:
    """Remove markdown code fences if the LLM wraps JSON in them."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


# ---------------------------------------------------------------------------
# Judge execution — Gemini
# ---------------------------------------------------------------------------
def _run_gemini_judge(
    workspace_id: str,
    model_name: str,
    page_images: List[str],
    prompt_text: str,
) -> Optional[str]:
    """Send images + prompt to Gemini and return raw response text."""
    model = _get_gemini_model(workspace_id, model_name)
    if not model:
        return None

    content_parts = []
    for b64_img in page_images:
        img_bytes = base64.b64decode(b64_img)
        content_parts.append(VertexImage.from_bytes(img_bytes))
    content_parts.append(prompt_text)

    log.info(f"📤 Sending {len(page_images)} image(s) + prompt to Gemini {model_name}...")
    response = model.generate_content(
        content_parts,
        generation_config={
            "temperature": 0.0,
            "max_output_tokens": 65536,
            "seed": 42,   # Fixed seed for reproducible validation results
        },
    )
    return response.text.strip()


# ---------------------------------------------------------------------------
# Judge execution — GPT (OpenAI)
# ---------------------------------------------------------------------------
def _run_gpt_judge(
    workspace_id: str,
    model_name: str,
    page_images: List[str],
    prompt_text: str,
) -> Optional[str]:
    """Send images + prompt to GPT and return raw response text."""
    client = _get_openai_client(workspace_id, model_name)
    if not client:
        return None

    # Build content array: images first, then text
    content = []
    for b64_img in page_images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{b64_img}",
                "detail": "high",
            },
        })
    content.append({"type": "text", "text": prompt_text})

    log.info(f"📤 Sending {len(page_images)} image(s) + prompt to GPT {model_name}...")
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        temperature=0.0,
        max_completion_tokens=32768,
        seed=42,  # Fixed seed for reproducible results across runs
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Main validation function
# ---------------------------------------------------------------------------
def validate(
    pdf_path: str,
    converted_text: str,
    state_code: str,
    golden_text: Optional[str] = None,
    judge: str = "gemini",               # "gemini" or "gpt"
    workspace_id: Optional[str] = None,
    gemini_model: str = _DEFAULT_GEMINI_MODEL,
    gpt_model: str = _DEFAULT_GPT_MODEL,
    max_pages: int = 20,
    dpi: int = 300,
    page_images: Optional[List[str]] = None,
    highlighted_images: Optional[List[str]] = None,
    pymupdf_report: Optional[Dict] = None,
) -> ValidationResult:
    """
    Run full LLM-as-Judge validation on a converted legislative bill.

    Args:
        pdf_path:           Path to the original PDF file
        converted_text:     The converted tagged text to validate
        state_code:         Two-letter state code (e.g. "KS", "CT", "FL")
        golden_text:        Optional golden reference for comparison scoring
        judge:              "gemini" or "gpt" — which LLM to use as judge
        workspace_id:       TR workspace ID (defaults to TR_WORKSPACE_ID)
        gemini_model:       Gemini model name
        gpt_model:          GPT model name
        max_pages:          Maximum PDF pages to send to the judge
        dpi:                Image rendering DPI
        page_images:        Pre-rendered base64 PNG images (skips PDF rendering
                            if provided — reuse images from the converter)
        highlighted_images: Base64 PNG images with PyMuPDF formatting overlays
                            (colour-coded: green=underline, red=strikethrough,
                            blue=italic, orange=bold). Sent alongside page_images
                            to help the judge see detected formatting.
        pymupdf_report:     PyMuPDF JSON report dict with formatted_spans.
                            When provided, these spans are used as GROUND TRUTH
                            for the GPT judge — GPT verifies each span instead
                            of trying to visually detect formatted text.

    Returns:
        ValidationResult with scores, issues, and detailed findings
    """
    ws_id = workspace_id or TR_WORKSPACE_ID
    state_code = state_code.upper()

    log.info("=" * 60)
    log.info(f"🔍 VALIDATION START — State: {state_code}, Judge: {judge}")
    log.info(f"   PDF:    {pdf_path}")
    log.info(f"   Output: {len(converted_text)} chars")
    log.info("=" * 60)

    result = ValidationResult(state_code=state_code)

    # ── Step 1: Structural analysis (regex, instant) ──────────────
    log.info("📊 Running structural pre-analysis...")
    stats = structural_analysis(converted_text)
    result.structural_stats = stats
    log.info(
        f"   Tags: {stats['addition_tags']} [A>], {stats['deletion_tags']} [D>] | "
        f"Words: {stats['word_count']} | Lines: {stats['line_count']}"
    )
    if stats["mismatched_additions"]:
        log.warning(f"   ⚠️  Mismatched [A> tags: {stats['open_a']} openers vs {stats['close_a']} closers")
    if stats["mismatched_deletions"]:
        log.warning(f"   ⚠️  Mismatched [D> tags: {stats['open_d']} openers vs {stats['close_d']} closers")
    if stats["nested_tags"]:
        log.warning(f"   ⚠️  Found {stats['nested_tags']} nested tag(s)")

    # ── Step 2: Golden comparison (if provided) ───────────────────
    golden_comp = None
    if golden_text:
        log.info("📏 Running golden reference comparison...")
        golden_comp = golden_comparison(converted_text, golden_text)
        result.golden_precision = golden_comp["overall"]["precision"]
        result.golden_recall    = golden_comp["overall"]["recall"]
        result.golden_f1        = golden_comp["overall"]["f1"]
        log.info(
            f"   Golden F1: {golden_comp['overall']['f1']:.1%} | "
            f"P: {golden_comp['overall']['precision']:.1%} | "
            f"R: {golden_comp['overall']['recall']:.1%}"
        )
        if golden_comp["dropped_word_count"] > 0:
            log.warning(f"   ⚠️  {golden_comp['dropped_word_count']} words in golden but not in output")

    # ── Step 3: Render PDF pages (or reuse pre-rendered) ─────────
    if page_images:
        log.info(f"🖼️  Reusing {len(page_images)} pre-rendered page image(s) from converter")
        page_images = page_images[:max_pages]
    else:
        log.info("🖼️  Rendering PDF pages...")
        page_images = pdf_to_images(pdf_path, dpi=dpi, max_pages=max_pages)
    if not page_images:
        log.error("❌ Could not render PDF pages — returning structural results only")
        result.overall_score_pct = 0
        result.confidence = "low"
        result.reasoning = "PDF rendering failed — only structural analysis available."
        return result
    result.pdf_pages = len(page_images)

    # ── Step 4: Build judge prompt ────────────────────────────────
    structural_summary = json.dumps({
        k: v for k, v in stats.items()
        if k not in ("addition_texts", "deletion_texts")
    }, indent=2)

    # Add golden discrepancies to structural info if available
    if golden_comp:
        structural_summary += "\n\nGOLDEN COMPARISON DISCREPANCIES:\n"
        if golden_comp["addition_tags"]["missing"]:
            structural_summary += f"  Missing additions (in golden, not in output): {golden_comp['addition_tags']['missing'][:10]}\n"
        if golden_comp["addition_tags"]["extra"]:
            structural_summary += f"  Extra additions (in output, not in golden): {golden_comp['addition_tags']['extra'][:10]}\n"
        if golden_comp["deletion_tags"]["missing"]:
            structural_summary += f"  Missing deletions: {golden_comp['deletion_tags']['missing'][:10]}\n"
        if golden_comp["deletion_tags"]["extra"]:
            structural_summary += f"  Extra deletions: {golden_comp['deletion_tags']['extra'][:10]}\n"

    state_config = load_state_config(state_code)

    # Build PyMuPDF ground truth checklist if report is provided
    pymupdf_checklist = ""
    pymupdf_span_count = 0
    python_segments: List[Dict] = []
    if pymupdf_report:
        pymupdf_checklist, pymupdf_span_count = _build_ground_truth_checklist(
            pymupdf_report, state_config
        )
        if pymupdf_span_count > 0:
            log.info(f"📋 PyMuPDF ground truth: {pymupdf_span_count} content span(s) for GPT to verify")
            # ── Programmatic matching (deterministic, no LLM) ─────
            python_segments = _programmatic_match_spans(
                pymupdf_report, converted_text, state_config
            )
            ok_py = sum(1 for s in python_segments if s.get("severity") == "OK")
            issue_py = len(python_segments) - ok_py
            log.info(f"🐍 Python exact match: {len(python_segments)} span(s) — "
                      f"{ok_py} OK, {issue_py} issues")
        else:
            log.warning("⚠️  PyMuPDF report provided but no content spans found")

    prompt_text = _build_judge_prompt(
        state_code=state_code,
        output_text=converted_text,
        n_pages=len(page_images),
        structural_info=structural_summary,
        golden_text=golden_text,
        state_config=state_config,
        has_highlighted_images=bool(highlighted_images),
        pymupdf_checklist=pymupdf_checklist,
        pymupdf_span_count=pymupdf_span_count,
    )

    # ── Step 5: Combine images (original + highlighted) ───────────
    all_images = list(page_images)
    if highlighted_images:
        hi_imgs = highlighted_images[:max_pages]
        all_images.extend(hi_imgs)
        log.info(f"🖼️  Sending {len(page_images)} original + {len(hi_imgs)} highlighted = {len(all_images)} total images")

    # ── Step 6: Call LLM judge ────────────────────────────────────
    try:
        if judge.lower() == "gpt":
            result.judge_model = gpt_model
            raw_response = _run_gpt_judge(ws_id, gpt_model, all_images, prompt_text)
        else:
            result.judge_model = gemini_model
            raw_response = _run_gemini_judge(ws_id, gemini_model, all_images, prompt_text)

        if not raw_response:
            log.error("❌ Judge returned no response")
            result.confidence = "low"
            result.reasoning = "LLM judge returned empty response."
            return result

        log.debug(f"Raw judge response (first 500): {raw_response[:500]}")

        # Parse JSON response
        cleaned = _strip_markdown_fences(raw_response)
        cleaned = _repair_truncated_json(cleaned)
        data = json.loads(cleaned)

        # Populate result from judge response
        result.missed_additions   = data.get("missed_additions", [])
        result.missed_deletions   = data.get("missed_deletions", [])
        result.false_additions    = data.get("false_additions", [])
        result.false_deletions    = data.get("false_deletions", [])
        result.dropped_text       = data.get("dropped_text", [])
        result.misplaced_tags     = data.get("misplaced_tags", [])
        result.split_merge_issues = data.get("split_merge_issues", [])
        result.layout_issues      = data.get("layout_issues", [])
        gpt_segments              = data.get("formatted_segments", [])
        result.tag_accuracy_pct   = float(data.get("tag_accuracy_pct", 0))
        result.content_accuracy_pct = float(data.get("content_accuracy_pct", 0))
        result.overall_score_pct  = float(data.get("overall_score_pct", 0))
        result.confidence         = data.get("confidence", "medium")
        result.reasoning          = data.get("reasoning", "")
        result.page_summaries     = data.get("page_summaries", [])

        # ── Merge: Python deterministic + GPT bracket/position results ──
        if python_segments:
            # Start with Python's deterministic results for visual spans
            merged = list(python_segments)

            # Add GPT's bracket / ALL CAPS / position segments
            # (anything GPT found that is NOT already covered by Python)
            python_texts = {_norm(s["text"]) for s in python_segments}
            for gs in gpt_segments:
                gs_text_norm = _norm(gs.get("text", ""))
                gs_fmt = gs.get("format_type", "").lower()
                # Keep GPT segments for text-based formatting (brackets, ALL CAPS)
                # or position issues that Python can't detect
                if gs_fmt in ("brackets", "all caps"):
                    gs["_source"] = "gpt"
                    merged.append(gs)
                elif gs_text_norm not in python_texts:
                    # GPT found something Python didn't — keep it
                    gs["_source"] = "gpt"
                    merged.append(gs)
                elif gs.get("severity", "OK").lower() not in ("ok", "correct"):
                    # GPT flagged a position issue for a Python span — update notes
                    for ps in merged:
                        if _norm(ps["text"]) == gs_text_norm and ps.get("_source") == "python":
                            if ps.get("severity") == "OK":
                                # GPT found a position issue Python couldn't see
                                ps["severity"] = gs.get("severity", "Medium")
                                ps["notes"] = f"Position: {gs.get('notes', '')}"
                                ps["suggested_fix"] = gs.get("suggested_fix", "")
                            break

            result.formatted_segments = merged
            log.info(f"   🔀 Merged: {len(python_segments)} Python + "
                      f"{len(merged) - len(python_segments)} GPT = {len(merged)} total")
        else:
            # No Python matching available — use GPT results as-is
            result.formatted_segments = gpt_segments

        # If judge returned page_summaries, validate/normalize them
        if result.page_summaries:
            for ps in result.page_summaries:
                ps.setdefault("page", 0)
                ps.setdefault("tag_accuracy_pct", 0.0)
                ps.setdefault("content_accuracy_pct", 0.0)
                ps.setdefault("page_score_pct", 0.0)
                ps.setdefault("issues_on_page", 0)
                ps.setdefault("page_notes", "")
                # Ensure numeric types
                for k in ("tag_accuracy_pct", "content_accuracy_pct", "page_score_pct"):
                    ps[k] = float(ps[k])
                ps["issues_on_page"] = int(ps["issues_on_page"])
            log.info(f"   📊 Page-by-page scores received for {len(result.page_summaries)} page(s)")
            # Log pages below 95%
            weak_pages = [ps for ps in result.page_summaries if ps["page_score_pct"] < 95]
            if weak_pages:
                for wp in weak_pages:
                    log.warning(
                        f"   ⚠️  Page {wp['page']}: {wp['page_score_pct']:.1f}% — {wp['page_notes'][:80]}"
                    )
            else:
                log.info(f"   ✅ All {len(result.page_summaries)} pages scored ≥95%")

        # Log formatted segments count
        if result.formatted_segments:
            ok_count = sum(1 for s in result.formatted_segments if str(s.get("severity", "")).lower().strip() == "ok")
            issue_count = len(result.formatted_segments) - ok_count
            log.info(f"   📋 {len(result.formatted_segments)} formatted segment(s): {ok_count} OK, {issue_count} with issues")

        log.info(f"✅ Judge complete — {result.summary().splitlines()[-3].strip()}")

    except json.JSONDecodeError as e:
        log.error(f"❌ Judge returned invalid JSON: {e}")
        log.error(f"   Raw (first 1000): {raw_response[:1000] if raw_response else '(empty)'}")
        result.confidence = "low"
        result.reasoning = f"Judge response was not valid JSON: {e}"
    except Exception as e:
        log.error(f"❌ Judge failed: {e}")
        result.confidence = "low"
        result.reasoning = f"Judge execution failed: {e}"

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="LLM-as-Judge Validation for Legislative PDF Conversion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python Validation.py bill.pdf output.txt --state KS
  python Validation.py bill.pdf output.txt --state CT --judge gpt
  python Validation.py bill.pdf output.txt --state FL --golden golden.txt
  python Validation.py bill.pdf output.txt --state KS --judge gemini --dpi 400

  # Full pipeline: validate + PyMuPDF ground-truth comparison in one command
  python Validation.py bill.pdf output.txt --state CT --judge gpt --excel report.xlsx --pymupdf-compare
        """,
    )
    parser.add_argument("pdf", help="Path to the original PDF file")
    parser.add_argument("converted", help="Path to the converted text output file")
    parser.add_argument("--state", required=True, help="Two-letter state code (e.g. KS, CT, FL)")
    parser.add_argument("--golden", help="Path to golden reference text file (optional)")
    parser.add_argument("--judge", choices=["gemini", "gpt"], default="gemini",
                        help="LLM judge to use (default: gemini)")
    parser.add_argument("--gemini-model", default=_DEFAULT_GEMINI_MODEL,
                        help=f"Gemini model name (default: {_DEFAULT_GEMINI_MODEL})")
    parser.add_argument("--gpt-model", default=_DEFAULT_GPT_MODEL,
                        help=f"GPT model name (default: {_DEFAULT_GPT_MODEL})")
    parser.add_argument("--workspace-id", default=TR_WORKSPACE_ID,
                        help="TR workspace ID")
    parser.add_argument("--max-pages", type=int, default=20,
                        help="Max PDF pages to send to judge (default: 20)")
    parser.add_argument("--dpi", type=int, default=300,
                        help="PDF rendering DPI (default: 300)")
    parser.add_argument("--output", help="Save detailed report to this file")
    parser.add_argument("--json-output", help="Save raw results as JSON to this file")
    parser.add_argument("--excel", help="Save results as formatted Excel report")
    parser.add_argument("--highlighted-dir",
                        help="Directory containing highlighted page images (PNG) "
                             "from PyMuPDF. These are sent alongside the original "
                             "PDF images to help the judge see formatting.")
    parser.add_argument("--pymupdf-report",
                        help="Path to PyMuPDF JSON report file. When provided, "
                             "formatted spans are used as ground truth for GPT "
                             "to verify instead of relying on visual detection.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")

    # ── PyMuPDF ground-truth comparison ──
    parser.add_argument("--pymupdf-compare", action="store_true",
                        help="Run PyMuPDF extraction + LLM clean + char-by-char "
                             "tag comparison against the converted output. "
                             "Generates a separate tag-comparison Excel.")
    parser.add_argument("--clean-model", default=None,
                        help="LLM model for PyMuPDF text cleaning "
                             "(default: gpt-4.1-mini)")
    parser.add_argument("--comparison-excel",
                        help="Output path for tag-comparison Excel. "
                             "Default: <excel>_tag_comparison.xlsx or "
                             "<pdf_stem>_tag_comparison.xlsx")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate inputs
    if not os.path.isfile(args.pdf):
        log.error(f"PDF file not found: {args.pdf}")
        sys.exit(1)
    if not os.path.isfile(args.converted):
        log.error(f"Converted text file not found: {args.converted}")
        sys.exit(1)

    # Read converted text (needed for both modes)
    with open(args.converted, "r", encoding="utf-8") as f:
        converted_text = f.read()

    # ══════════════════════════════════════════════════════════════════
    # MODE 1: PyMuPDF ground-truth comparison (standalone, no judge)
    # ══════════════════════════════════════════════════════════════════
    if args.pymupdf_compare:
        log.info("🔬 PyMuPDF ground-truth comparison mode")
        log.info("   PDF:       %s", args.pdf)
        log.info("   Converted: %d chars", len(converted_text))
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
            from pymupdf_extractor import extract_pdf_with_pymupdf, clean_extracted_text
            from compare_tags import generate_tag_comparison_excel

            # Step 1: Extract raw text with PyMuPDF
            log.info("📄 Step 1: Extracting text with PyMuPDF …")
            raw_text = extract_pdf_with_pymupdf(
                args.pdf, join_pages=True,
                mark_underline=True, mark_strikethrough=True,
            )
            log.info("   Raw: %d chars", len(raw_text))

            # Step 2: LLM-clean the extracted text (Claude)
            log.info("🧹 Step 2: Cleaning with Claude …")
            # Load state config for deletions field
            from Validation import load_state_config as _load_sc
            _state_cfg = _load_sc(args.state) if args.state else None
            clean_text = clean_extracted_text(
                raw_text, state_config=_state_cfg,
            )
            log.info("   Clean: %d chars", len(clean_text))

            # Save cleaned text alongside the PDF for reference
            pdf_stem = Path(args.pdf).stem
            clean_txt_path = Path(args.pdf).parent / f"{pdf_stem}_pymupdf_clean.txt"
            clean_txt_path.write_text(clean_text, encoding="utf-8")
            log.info("   Saved clean text: %s", clean_txt_path)

            # Step 3: Generate tag comparison Excel
            log.info("📊 Step 3: Generating tag comparison Excel …")
            state_lc = args.state.lower()
            if args.comparison_excel:
                cmp_excel = args.comparison_excel
            elif args.excel:
                cmp_excel = args.excel
            else:
                cmp_excel = str(Path(args.pdf).parent / f"{state_lc}_validation_full_tag_comparison.xlsx")

            cmp_path = generate_tag_comparison_excel(
                clean_text, converted_text, cmp_excel,
            )
            if cmp_path:
                log.info("✅ Tag comparison Excel saved to %s", cmp_path)
            else:
                log.error("Failed to generate tag comparison Excel")
                sys.exit(1)

        except ImportError as e:
            log.error("Cannot run PyMuPDF comparison: %s", e)
            log.error("Make sure PyMuPDF (fitz) is installed: pip install pymupdf")
            sys.exit(1)
        except Exception as e:
            log.error("PyMuPDF comparison failed: %s", e, exc_info=True)
            sys.exit(1)

        sys.exit(0)

    # ══════════════════════════════════════════════════════════════════
    # MODE 2: LLM-as-Judge validation (original flow)
    # ══════════════════════════════════════════════════════════════════
    golden_text = None
    if args.golden:
        if not os.path.isfile(args.golden):
            log.error(f"Golden reference file not found: {args.golden}")
            sys.exit(1)
        with open(args.golden, "r", encoding="utf-8") as f:
            golden_text = f.read()

    # Load highlighted images if provided
    highlighted_images = None
    if args.highlighted_dir and os.path.isdir(args.highlighted_dir):
        import glob
        hi_files = sorted(glob.glob(os.path.join(args.highlighted_dir, "page_*.png")))
        if hi_files:
            highlighted_images = []
            for hf in hi_files:
                with open(hf, "rb") as img_f:
                    highlighted_images.append(base64.b64encode(img_f.read()).decode("utf-8"))
            log.info(f"📷 Loaded {len(highlighted_images)} highlighted image(s) from {args.highlighted_dir}")

    # Load PyMuPDF report if provided
    pymupdf_report = None
    pymupdf_report_path = getattr(args, 'pymupdf_report', None)
    if pymupdf_report_path and os.path.isfile(pymupdf_report_path):
        with open(pymupdf_report_path, "r", encoding="utf-8") as f:
            pymupdf_report = json.load(f)
        n_spans = len(pymupdf_report.get("formatted_spans", []))
        log.info(f"📋 Loaded PyMuPDF report: {n_spans} formatted span(s) from {pymupdf_report_path}")

    # Run validation
    result = validate(
        pdf_path=args.pdf,
        converted_text=converted_text,
        state_code=args.state,
        golden_text=golden_text,
        judge=args.judge,
        workspace_id=args.workspace_id,
        gemini_model=args.gemini_model,
        gpt_model=args.gpt_model,
        max_pages=args.max_pages,
        dpi=args.dpi,
        highlighted_images=highlighted_images,
        pymupdf_report=pymupdf_report,
    )

    # Print report
    report = result.detailed_report()
    print("\n" + report)

    # Save report to file
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        log.info(f"📄 Report saved to {args.output}")

    # Save JSON
    if args.json_output:
        json_data = {
            "state_code": result.state_code,
            "judge_model": result.judge_model,
            "pdf_pages": result.pdf_pages,
            "confidence": result.confidence,
            "tag_accuracy_pct": result.tag_accuracy_pct,
            "content_accuracy_pct": result.content_accuracy_pct,
            "overall_score_pct": result.overall_score_pct,
            "grade": result.grade(),
            "golden_precision": result.golden_precision,
            "golden_recall": result.golden_recall,
            "golden_f1": result.golden_f1,
            "structural_stats": result.structural_stats,
            "total_issues": result.total_issues(),
            "page_summaries": result.page_summaries,
            "missed_additions": result.missed_additions,
            "missed_deletions": result.missed_deletions,
            "false_additions": result.false_additions,
            "false_deletions": result.false_deletions,
            "dropped_text": result.dropped_text,
            "misplaced_tags": result.misplaced_tags,
            "split_merge_issues": result.split_merge_issues,
            "layout_issues": result.layout_issues,
            "formatted_segments": result.formatted_segments,
            "reasoning": result.reasoning,
        }
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        log.info(f"📄 JSON results saved to {args.json_output}")

    # Save Excel
    excel_out = args.excel
    if not excel_out:
        state_lc = args.state.lower()
        excel_out = str(Path(args.pdf).parent / f"{state_lc}_validation_report.xlsx")
    state_cfg = load_state_config(args.state)
    excel_path = generate_validation_excel(result, excel_out, state_config=state_cfg)
    if excel_path:
        log.info(f"📊 Excel validation report saved to {excel_path}")

    # Exit code based on grade
    if result.grade() in ("A+", "A", "B+", "B"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
