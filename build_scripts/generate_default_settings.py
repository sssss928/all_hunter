from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

import settings  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_default_settings.py OUTPUT_PATH")

    output_path = Path(sys.argv[1]).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(settings.get_default_config(), ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

