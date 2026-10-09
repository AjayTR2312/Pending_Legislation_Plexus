"""
Puts the project's app/ folder on sys.path and re-exports the shared settings module, so every
backend module reads configuration the same way:

    from appconfig import settings
    settings.API_PORT

All values, their defaults and what each one does:  python app/settings.py --show
"""

import sys
from pathlib import Path

_APP = str(Path(__file__).resolve().parent.parent.parent / "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import settings  # noqa: E402

__all__ = ["settings"]
