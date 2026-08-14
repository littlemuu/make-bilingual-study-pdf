#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from adapters.base import AdapterError
from adapters.mineru import import_mineru


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import a frozen MinerU 3.x pipeline output directory without running MinerU."
        )
    )
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("mineru_output_dir", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--render-dpi", type=int, default=120)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = import_mineru(
            args.source_pdf,
            args.mineru_output_dir,
            args.work_dir,
            args.profile,
            render_dpi=args.render_dpi,
            force=args.force,
        )
    except AdapterError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
