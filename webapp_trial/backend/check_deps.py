"""Report which requirements.txt entries are not installed in this interpreter."""

import importlib.metadata as md
import re
import sys
from pathlib import Path

REQ = Path(__file__).resolve().parent.parent.parent / "requirements.txt"


def main() -> int:
    missing = []
    declared = 0
    for line in REQ.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!\[;]", line)[0].strip()
        if not name:
            continue
        declared += 1
        try:
            md.version(name)
        except md.PackageNotFoundError:
            missing.append(name)

    print(f"declared: {declared}")
    print("missing:", ", ".join(missing) if missing else "none — all installed")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
