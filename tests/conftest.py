"""
pytest configuration and shared fixtures.

The legislative-PDF extractor lives in the ``app/`` directory but is not
packaged (no ``__init__.py``).  We extend ``sys.path`` here so tests can
``import pymupdf_extractor`` etc. directly without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``app/`` importable from any test module.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_DIR = _REPO_ROOT / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))
