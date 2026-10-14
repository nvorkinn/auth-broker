"""Command line: `python -m tools.openapi_codegen SPEC --out DIR [--check]`."""

import argparse
import sys
from pathlib import Path

from .generate import generate, load, stale_files, write_files
from .schema import SpecError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.openapi_codegen",
        description=(
            "Generate Pydantic models, requests clients and Flask blueprints from an OpenAPI 3.x JSON or YAML file."
        ),
    )
    parser.add_argument("spec", type=Path, help="the OpenAPI file to read: .json, or .yaml/.yml (needs PyYAML)")
    parser.add_argument("--out", type=Path, required=True, help="the package directory to write")
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if the files under --out differ from what would be generated",
    )
    args = parser.parse_args(argv)

    try:
        files = generate(load(args.spec), source=args.spec.name)
    except (SpecError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.check:
        stale = stale_files(files, args.out)
        for relative in stale:
            print(f"out of date: {args.out / relative}", file=sys.stderr)
        return 1 if stale else 0

    removed = write_files(files, args.out)
    for relative in removed:
        print(f"removed {args.out / relative}")
    print(f"Wrote {len(files)} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
