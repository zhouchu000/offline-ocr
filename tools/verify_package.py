"""CLI wrapper for offline package integrity verification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from offline_word.package_verify import verify


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        result = verify(args.folder)
        print(json.dumps(result, ensure_ascii=True))
        raise SystemExit(0 if result['pass'] else 1)
    except (OSError, ValueError, TypeError):
        print('package_verification_failed')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
