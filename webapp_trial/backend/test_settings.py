"""The central settings registry: defaults must equal the values that used to be hardcoded."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
import settings as st  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent

# Every value that used to be a literal in the code. If this table passes, moving the literal
# into the registry changed no behaviour for anyone who has not set an override.
LEGACY = {
    "SFTP_HOST": "10.17.38.130", "SFTP_PORT": 22, "SFTP_USER": "mag2ftp",
    "SFTP_UPLOAD_DIR": "/apps/Codes/MP2/LandingStrips/LCPR/ftp/keyed",
    "SFTP_DOWNLOAD_DIR": "/apps/Codes/MP2/LandingStrips/LCPR/ftp/out",
    "TR_WORKSPACE_ID": "AgentTraininJaH4",
    "TR_ANTHROPIC_TOKEN_URL": "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token",
    "TOKEN_RETRY_DELAYS_S": (5, 15, 30),
    "LLM_MAX_TOKENS_CLEAN": 16384, "LLM_MAX_TOKENS_STREAM": 128000, "LLM_MAX_TOKENS_CHUNK": 64000,
    "LLM_MAX_TOKENS_VERIFY": 8000, "LLM_MAX_TOKENS_PING": 100, "LLM_CHUNK_TIMEOUT_S": 600.0,
    "STREAMLIT_MAX_BATCH_FILES": 20, "BATCH_MAX_TOTAL_SIZE_MB": 50, "MAX_PAGES_PYMUPDF": 15,
    "MAX_PAGES_VISION": 50, "WEBAPP_MAX_BATCH_FILES": 50, "PIPELINE_POLL_MINUTES": 5, "VISUAL_MAX_DPI": 600,
    "AUDIT_CHECKLIST_CHUNK_SIZE": 250, "AUDIT_MAX_IMAGES_PER_CHUNK": 20, "AUDIT_MAX_IMAGE_DIM": 2000,
    "VERIFY_MAX_DIFFS_PER_BATCH": 40, "VERIFY_MAX_IMAGES_PER_CALL": 12, "VERIFY_MAX_AUTO_DEMOTE_WORDS": 15,
    "API_PORT": 8800, "UI_URL": "http://localhost:5173", "UI_ORIGINS": ["http://localhost:5173"],
    "JOB_STALE_SECONDS": 1800, "DB_TIMEOUT_S": 30.0, "WORKER_POLL_SECONDS": 2.0, "JOBLOG_MAX_ENTRIES": 1000,
    "COMPARE_RENDER_DPI": 150, "REVIEW_MAX_BOXES": 60, "REVIEW_MAX_REVISIONS": 1500, "RULES_MAX_PER_RULE": 40,
    "LLM_MODEL": "claude-opus-5-5", "LLM_FALLBACK_MODEL": "claude-opus-4-7",
    "LLM_PRIMARY_ATTEMPTS": 3, "LLM_LAST_ATTEMPTS": 5,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The real .env is loaded at import; clear every registered name so defaults are what we test."""
    for s in st.REGISTRY:
        monkeypatch.delenv(s.name, raising=False)


def test_defaults_equal_the_old_hardcoded_values():
    for name, expected in LEGACY.items():
        assert st.get(name) == expected, name


def test_every_registered_setting_has_a_documented_default_that_casts():
    for s in st.REGISTRY:
        st.get(s.name)  # must not raise
        assert s.doc and s.group


def test_env_overrides_and_empty_means_unset(monkeypatch):
    monkeypatch.setenv("SFTP_PORT", "2222")
    monkeypatch.setenv("SFTP_HOST", "   ")  # blank -> default
    assert st.SFTP_PORT == 2222 and st.SFTP_HOST == "10.17.38.130"


def test_casting_each_kind(monkeypatch):
    monkeypatch.setenv("LLM_CHUNK_TIMEOUT_S", "12.5")
    monkeypatch.setenv("UI_ORIGINS", "http://a:1, http://b:2 ,")
    monkeypatch.setenv("TOKEN_RETRY_DELAYS_S", "1,2")
    monkeypatch.setenv("PROMPTS_DIR", "my/prompts")
    assert st.LLM_CHUNK_TIMEOUT_S == 12.5
    assert st.UI_ORIGINS == ["http://a:1", "http://b:2"]
    assert st.TOKEN_RETRY_DELAYS_S == (1, 2)
    assert st.PROMPTS_DIR == ROOT / "my" / "prompts"  # relative paths resolve against the project


def test_a_bad_value_fails_loudly_and_names_the_setting(monkeypatch):
    monkeypatch.setenv("SFTP_PORT", "twenty-two")
    with pytest.raises(st.SettingsError, match="SFTP_PORT"):
        st.get("SFTP_PORT")
    monkeypatch.setenv("TOKEN_RETRY_DELAYS_S", "5,soon")
    with pytest.raises(st.SettingsError, match="TOKEN_RETRY_DELAYS_S"):
        st.get("TOKEN_RETRY_DELAYS_S")


def test_derived_defaults_follow_the_tmp_root(monkeypatch, tmp_path):
    assert st.UPLOAD_SOURCE_DIR.name == "pending_processing" and st.DOWNLOAD_DEST_DIR.name == "pending_downloads"
    monkeypatch.setenv("APP_TMP_DIR", str(tmp_path))
    assert st.UPLOAD_SOURCE_DIR == tmp_path / "pending_processing"
    assert st.DOWNLOAD_DEST_DIR == tmp_path / "pending_downloads"
    monkeypatch.setenv("TRIAL_STORAGE_DIR", str(tmp_path / "store"))
    assert st.TRIAL_DB_PATH == tmp_path / "store" / "jobs.db"


def test_no_default_points_into_someone_elses_home_directory():
    for name in ("UPLOAD_SOURCE_DIR", "DOWNLOAD_DEST_DIR", "TRIAL_STORAGE_DIR", "APP_TMP_DIR"):
        assert "/home/" not in str(st.get(name)).replace("\\", "/")


def test_secrets_are_masked_in_the_description(monkeypatch):
    monkeypatch.setenv("FTP_PASSWORD", "hunter2-super-secret")
    monkeypatch.setenv("CIAM_CLIENT_SECRET", "another-secret-value")
    shown = {r["name"]: r for r in st.describe()}
    assert shown["FTP_PASSWORD"]["value"] == "<set>" and shown["CIAM_CLIENT_ID"]["value"] == "<not set>"
    blob = repr(st.describe()) + st.render_env_example()
    assert "hunter2-super-secret" not in blob and "another-secret-value" not in blob


def test_attribute_access_and_unknown_names():
    assert st.get("SFTP_USER") == st.SFTP_USER == "mag2ftp"
    with pytest.raises(AttributeError):
        st.NOT_A_SETTING
    with pytest.raises(KeyError):
        st.get("NOT_A_SETTING")


def test_env_example_is_in_sync_with_the_registry():
    """Fails when someone adds a setting without regenerating the docs:  python app/settings.py --write-example"""
    on_disk = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert on_disk == st.render_env_example()


def test_the_example_file_carries_no_secret_values():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0]
        if name in {s.name for s in st.REGISTRY if s.secret}:
            assert line == f"{name}="


def test_pricing_file_exists_and_covers_every_default_model():
    import json

    sheet = json.loads((ROOT / "config" / "model_pricing.json").read_text(encoding="utf-8"))["models"]
    assert st.LLM_MODEL in sheet and st.LLM_FALLBACK_MODEL in sheet
    assert all({"input", "output", "cache_write", "cache_read"} <= set(p) for p in sheet.values())
