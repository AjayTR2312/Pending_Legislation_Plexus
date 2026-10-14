"""
Every deployment-specific value and operational tunable, declared ONCE.

    import settings
    settings.SFTP_HOST            # attribute access, always the current value
    settings.get("LLM_MODEL")     # same thing by name

Resolution order for each setting:  process environment  >  project .env  >  default below.
An empty value counts as "not set". A value of the wrong type fails loudly with the setting's
name (it never silently falls back), so a typo cannot quietly change production behaviour.

    python app/settings.py --show            effective values, where each came from (secrets masked)
    python app/settings.py --check           validate every value; non-zero exit if any is invalid
    python app/settings.py --write-example   regenerate .env.example from this file

What lives here: models, endpoints, hosts, credentials, directories, limits, timeouts, batch
sizes, ports. What deliberately does NOT: PDF-extraction heuristics (column gaps, margin cut-offs),
regexes, prompts and UI wording — those are algorithm, not configuration; changing them is a code
change that needs a test, not an environment edit.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("settings")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
EXAMPLE_FILE = PROJECT_ROOT / ".env.example"

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=ENV_FILE, override=False)
except ImportError:  # python-dotenv is optional; the process environment still works
    pass


class SettingsError(ValueError):
    """A setting has a value that cannot be used."""


@dataclass(frozen=True)
class Setting:
    name: str
    default: Any
    kind: str  # str | int | float | bool | path | list | ints
    group: str
    doc: str
    secret: bool = False
    derived: Optional[Callable[[], Any]] = None  # computed default (shown as "derived" in docs)


# ── helpers for derived defaults ─────────────────────────────────────────────

def tesseract_dir() -> str:
    """Folder holding tesseract(.exe), or "" when OCR is not installed.

    Order: TESSERACT_DIR (.env) > whatever is on PATH > the usual Windows install folders."""
    import shutil
    configured = (_raw_env("TESSERACT_DIR") or "").strip()
    if configured and Path(configured).is_dir():
        return configured
    found = shutil.which("tesseract")
    if found:
        return str(Path(found).parent)
    for base in (os.environ.get("LOCALAPPDATA", "") and Path(os.environ["LOCALAPPDATA"]) / "Programs",
                 Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                 Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))):
        candidate = Path(base) / "Tesseract-OCR" if base else None
        if candidate and candidate.is_dir():
            return str(candidate)
    return ""


def tmp_root() -> Path:
    """Writable scratch root: APP_TMP_DIR > <project>/tmp > /tmp/pending_legislation."""
    configured = os.environ.get("APP_TMP_DIR", "").strip()
    if configured:
        return Path(configured)
    candidate = PROJECT_ROOT / "tmp"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        fallback = Path("/tmp/pending_legislation")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


REGISTRY: list[Setting] = []


def _s(name: str, default: Any, kind: str, group: str, doc: str, secret: bool = False,
       derived: Optional[Callable[[], Any]] = None) -> None:
    REGISTRY.append(Setting(name, default, kind, group, doc, secret, derived))


# ── AI models ────────────────────────────────────────────────────────────────
G = "AI models"
_s("LLM_MODEL", "claude-opus-5-5", "str", G, "Model every call asks for.")
_s("LLM_FALLBACK_MODEL", "claude-opus-4-7", "str", G,
   "Used when LLM_MODEL is unavailable, overloaded or erroring. Set to `none` to disable the fallback.")
_s("LLM_PRICING_FILE", "config/model_pricing.json", "path", G,
   "USD-per-1M-token price sheet used for cost reports. Edit the file when prices change.")
_s("LLM_PROMPT_CACHE", True, "bool", G,
   "Cache large system prompts (the state prompts). A hit reads them at the cached rate (~5% of input price); "
   "a miss pays 1.25x on that part. Auto-disables itself if the gateway rejects cache markers.")
_s("LLM_CACHE_AUTO", True, "bool", G,
   "Only cache when it will pay off: another job for the same state is waiting, or the cache is already warm. "
   "A lone document pays ~25% extra on the cached part and nothing ever reads it. false = always cache (when LLM_PROMPT_CACHE is on).")
_s("LLM_CACHE_WARM_S", 240, "int", G, "How long a cached prompt counts as warm (the API keeps it 5 minutes).")
_s("LLM_CACHE_MIN_CHARS", 4000, "int", G,
   "Only system prompts at least this long are cached (the API ignores caching below ~1,000-4,000 tokens).")
_s("LLM_PRIMARY_ATTEMPTS", 3, "int", G, "Tries on the primary model when it answers 'overloaded', before switching.")
_s("LLM_LAST_ATTEMPTS", 5, "int", G, "Tries on the last model in the chain (nowhere left to fall back to).")
_s("LLM_MAX_TOKENS_CLEAN", 16384, "int", G, "max_tokens for the hybrid text-clean call.")
_s("LLM_MAX_TOKENS_STREAM", 128000, "int", G, "max_tokens for streamed visual conversion, reconcile, self-correct and tag audit.")
_s("LLM_MAX_TOKENS_CHUNK", 64000, "int", G, "max_tokens for per-chunk hybrid conversion calls.")
_s("LLM_MAX_TOKENS_VERIFY", 8000, "int", G, "max_tokens for the diff-verifier call.")
_s("LLM_MAX_TOKENS_PING", 100, "int", G, "max_tokens for the tiny connectivity / smoke-test call.")
_s("LLM_CHUNK_TIMEOUT_S", 600.0, "float", G, "Request timeout (seconds) for large chunk calls.")

# ── AI platform auth ─────────────────────────────────────────────────────────
G = "AI platform auth"
_s("CIAM_CLIENT_ID", "", "str", G, "CIAM M2M client id. With CIAM_CLIENT_SECRET + ASSET_ID, CIAM auth is tried first.", secret=True)
_s("CIAM_CLIENT_SECRET", "", "str", G, "CIAM M2M client secret.", secret=True)
_s("ASSET_ID", "", "str", G, "CIAM asset id.", secret=True)
_s("TR_WORKSPACE_ID", "AgentTraininJaH4", "str", G, "Workspace id for the token-exchange fallback.")
_s("TR_ANTHROPIC_TOKEN_URL", "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token", "str", G,
   "Token endpoint for the workspace fallback.")
_s("TOKEN_TIMEOUT_S", 60, "int", G, "Timeout (seconds) for a token request.")
_s("TOKEN_RETRY_DELAYS_S", (5, 15, 30), "ints", G,
   "Comma list of waits between token-request tries on timeouts / connection errors / 5xx.")

# ── SFTP (Keyed) ─────────────────────────────────────────────────────────────
G = "SFTP / Keyed"
_s("SFTP_HOST", "10.17.38.130", "str", G, "SFTP server.")
_s("SFTP_PORT", 22, "int", G, "SFTP port.")
_s("SFTP_USER", "mag2ftp", "str", G, "SFTP user.")
_s("FTP_PASSWORD", "", "str", G, "SFTP password. Required to download or submit.", secret=True)
_s("SFTP_UPLOAD_DIR", "/apps/Codes/MP2/LandingStrips/LCPR/ftp/keyed", "str", G, "Remote folder 'Submit to Keyed' uploads to.")
_s("SFTP_DOWNLOAD_DIR", "/apps/Codes/MP2/LandingStrips/LCPR/ftp/out", "str", G, "Remote folder the pipeline downloads from.")

# ── Folders and files ────────────────────────────────────────────────────────
G = "Folders and files"
_s("APP_TMP_DIR", "", "path", G, "Scratch root for sessions and downloads.",
   derived=lambda: PROJECT_ROOT / "tmp")
_s("UPLOAD_SOURCE_DIR", "", "path", G, "Local folder ftp_upload.py scans for output/*.txt.",
   derived=lambda: tmp_root() / "pending_processing")
_s("DOWNLOAD_DEST_DIR", "", "path", G, "Local folder ftp_download.py saves into.",
   derived=lambda: tmp_root() / "pending_downloads")
_s("TRIAL_STORAGE_DIR", "", "path", G, "React trial app storage (jobs DB, edits, approved copies).",
   derived=lambda: PROJECT_ROOT / "webapp_trial" / "backend" / "storage")
_s("TRIAL_DB_PATH", "", "path", G, "SQLite job database.",
   derived=lambda: Path(os.environ.get("TRIAL_STORAGE_DIR", "").strip() or PROJECT_ROOT / "webapp_trial" / "backend" / "storage") / "jobs.db")
_s("TESSERACT_DIR", "", "str", G, "Folder containing tesseract (OCR). Blank = find it on PATH or in the usual install folders.")
_s("PROMPTS_DIR", "data/Prompts", "path", G, "State clean-prompt files ({ST}_Clean_Prompt.txt).")
_s("STATE_SPEC_FILE", "Opus4.6/netscan_state_template.json", "path", G, "NetScan per-state markup spec.")

# ── Limits ───────────────────────────────────────────────────────────────────
G = "Limits"
_s("STREAMLIT_MAX_BATCH_FILES", 20, "int", G, "Files per batch in the Streamlit app.")
_s("BATCH_MAX_TOTAL_SIZE_MB", 50, "int", G, "Total upload size per batch (Streamlit).")
_s("MAX_PAGES_PYMUPDF", 15, "int", G, "Page limit for PyMuPDF / hybrid conversion.")
_s("MAX_PAGES_VISION", 50, "int", G, "Page limit for vision (scanned) conversion.")
_s("WEBAPP_MAX_BATCH_FILES", 50, "int", G, "Files per batch in the React app.")
_s("PIPELINE_POLL_MINUTES", 5, "int", G, "Default poll interval of pipeline_runner --poll.")
_s("VISUAL_MAX_DPI", 600, "int", G, "Safety cap on page-image DPI for vision conversion.")

# ── Audit and verification ───────────────────────────────────────────────────
G = "Audit and verification"
_s("AUDIT_CHECKLIST_CHUNK_SIZE", 250, "int", G, "Tag-audit checklist items per API call.")
_s("AUDIT_MAX_IMAGES_PER_CHUNK", 20, "int", G, "Page images per tag-audit call (keeps requests under the size limit).")
_s("AUDIT_MAX_IMAGE_DIM", 2000, "int", G, "Longest side (px) of an audit page image.")
_s("VERIFY_MAX_DIFFS_PER_BATCH", 40, "int", G, "Differences per diff-verifier call.")
_s("VERIFY_MAX_IMAGES_PER_CALL", 12, "int", G, "Page images per diff-verifier call.")
_s("VERIFY_MAX_AUTO_DEMOTE_WORDS", 15, "int", G, "Largest difference (words) the verifier may auto-demote.")

# ── React app and API ────────────────────────────────────────────────────────
G = "React app and API"
_s("API_PORT", 8800, "int", G, "Port the FastAPI backend listens on (the UI proxy points here).")
_s("UI_URL", "http://localhost:5173", "str", G, "Where the React UI is served.")
_s("UI_ORIGINS", ["http://localhost:5173"], "list", G, "Comma list of origins allowed by CORS.")
_s("JOB_STALE_SECONDS", 1800, "int", G, "A 'processing' job untouched this long is returned to the queue.")
_s("DB_TIMEOUT_S", 30.0, "float", G, "SQLite lock wait (seconds).")
_s("WORKER_POLL_SECONDS", 2.0, "float", G, "How often worker.py looks for queued jobs.")
_s("JOBLOG_MAX_ENTRIES", 1000, "int", G, "Activity-log lines kept per job.")
_s("COMPARE_RENDER_DPI", 150, "int", G, "DPI of the PDF page images in the review screen.")
_s("REVIEW_MAX_BOXES", 60, "int", G, "PDF boxes drawn per finding.")
_s("REVIEW_MAX_REVISIONS", 1500, "int", G, "Revisions listed per document.")
_s("RULES_MAX_PER_RULE", 40, "int", G, "Findings kept per validation rule.")

_BY_NAME: dict[str, Setting] = {}
for _setting in REGISTRY:
    if _setting.name in _BY_NAME:
        raise RuntimeError(f"duplicate setting {_setting.name}")
    _BY_NAME[_setting.name] = _setting


# ── Reading and casting ──────────────────────────────────────────────────────

def _cast(setting: Setting, raw: Any) -> Any:
    kind = setting.kind
    try:
        if kind == "str":
            return str(raw).strip()
        if kind == "int":
            return int(str(raw).strip())
        if kind == "float":
            return float(str(raw).strip())
        if kind == "bool":
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected true/false")
        if kind == "path":
            p = Path(str(raw).strip()).expanduser()
            return p if p.is_absolute() else PROJECT_ROOT / p
        if kind == "list":
            if isinstance(raw, (list, tuple)):
                return list(raw)
            return [part.strip() for part in str(raw).split(",") if part.strip()]
        if kind == "ints":
            if isinstance(raw, (list, tuple)):
                return tuple(int(x) for x in raw)
            return tuple(int(part) for part in str(raw).split(",") if part.strip())
    except (TypeError, ValueError) as err:
        raise SettingsError(f"{setting.name}={raw!r} is invalid: expected {kind} ({err})") from None
    raise SettingsError(f"{setting.name}: unknown kind {kind!r}")


def _raw_env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value is not None and value.strip() else None


def get(name: str) -> Any:
    """Current value of a setting (environment > .env > default)."""
    try:
        setting = _BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown setting {name!r}") from None
    raw = _raw_env(name)
    if raw is not None:
        return _cast(setting, raw)
    if setting.derived is not None:
        return setting.derived()
    return _cast(setting, setting.default)


def __getattr__(name: str) -> Any:  # settings.SFTP_HOST
    if name in _BY_NAME:
        return get(name)
    raise AttributeError(f"module 'settings' has no attribute {name!r}")


# ── Documentation and diagnostics ────────────────────────────────────────────

def _source(setting: Setting) -> str:
    if _raw_env(setting.name) is None:
        return "default"
    try:
        from dotenv import dotenv_values

        from_file = (dotenv_values(ENV_FILE).get(setting.name) or "").strip()
        if from_file and from_file == os.environ.get(setting.name, "").strip():
            return ".env"
    except ImportError:
        pass
    return "environment"


def _display(setting: Setting, value: Any) -> str:
    if setting.secret:
        return "<set>" if value not in ("", None) else "<not set>"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def describe() -> list[dict[str, Any]]:
    """Effective configuration, secrets masked."""
    rows = []
    for setting in REGISTRY:
        try:
            value, error = get(setting.name), None
        except SettingsError as err:
            value, error = None, str(err)
        rows.append({
            "name": setting.name, "group": setting.group, "doc": setting.doc, "kind": setting.kind,
            "value": "<invalid>" if error else _display(setting, value),
            "source": _source(setting), "secret": setting.secret, "error": error,
            "default": "(derived)" if setting.derived else _display(setting, _cast(setting, setting.default)
                                                                    if setting.default != "" else ""),
        })
    return rows


def render_env_example() -> str:
    """The text of .env.example — generated, so it can never drift from this file."""
    out = [
        "# Copy to .env and fill in the secrets. Every setting is optional unless noted: the value shown",
        "# is the built-in default. Generated by `python app/settings.py --write-example`; edit settings.py,",
        "# not this file. Resolution order: process environment > .env > default.",
    ]
    group = None
    for setting in REGISTRY:
        if setting.group != group:
            group = setting.group
            out += ["", f"# ── {group} " + "─" * max(4, 66 - len(group))]
        out.append(f"# {setting.doc}")
        if setting.secret:
            out.append(f"{setting.name}=")
        elif setting.derived:
            out.append(f"# {setting.name}=            (default: derived from the project folder)")
        else:
            default = setting.default
            text = ",".join(str(v) for v in default) if isinstance(default, (list, tuple)) else str(default)
            out.append(f"{setting.name}={text}")
    return "\n".join(out) + "\n"


def _main(argv: list[str]) -> int:
    if "--write-example" in argv:
        EXAMPLE_FILE.write_text(render_env_example(), encoding="utf-8", newline="\n")
        print(f"wrote {EXAMPLE_FILE}")
        return 0
    rows = describe()
    bad = [r for r in rows if r["error"]]
    if "--check" in argv:
        for r in bad:
            print("INVALID", r["error"])
        missing = [r["name"] for r in rows if r["secret"] and r["value"] == "<not set>"
                   and r["name"] in ("FTP_PASSWORD",)]
        for name in missing:
            print(f"note: {name} is not set (needed to download from / submit to Keyed)")
        print(f"{len(rows)} settings, {len(bad)} invalid")
        return 1 if bad else 0
    group = None
    for r in rows:
        if r["group"] != group:
            group = r["group"]
            print(f"\n[{group}]")
        flag = "" if r["source"] == "default" else f"   <- {r['source']}"
        print(f"  {r['name']:30} {r['value']}{flag}")
    print("\n(secrets are masked; `--check` validates, `--write-example` refreshes .env.example)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
