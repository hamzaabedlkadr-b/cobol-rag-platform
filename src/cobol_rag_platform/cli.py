from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cobol_rag_platform.config import ConfigurationError, load_platform, load_program
from cobol_rag_platform.pipeline import Pipeline, PipelineError, STAGES


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="cobol-platform",
        description="Run COBOL analysis, optional cobol-rekt enrichment, and RAG indexing from one manifest.",
    )
    root.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("COBOL_PLATFORM_CONFIG", "config/platform.toml")),
        help="Platform TOML configuration.",
    )
    root.add_argument(
        "--programs-dir",
        type=Path,
        default=Path(os.getenv("COBOL_PLATFORM_PROGRAMS_DIR", "programs")),
        help="Directory containing PROGRAM/program.toml manifests.",
    )
    root.add_argument(
        "--runs-dir",
        type=Path,
        default=Path(os.getenv("COBOL_PLATFORM_RUNS_DIR", ".runs")),
        help="Generated run, cache, index, and state directory.",
    )
    commands = root.add_subparsers(dest="command", required=True)

    for name in ("doctor", "plan", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("program")

    run = commands.add_parser("run")
    run.add_argument("program")
    run.add_argument("--force", action="store_true", help="Ignore stage fingerprints and rerun completed stages.")
    run.add_argument("--dry-run", action="store_true", help="Print commands without writing or executing them.")
    run.add_argument("--stop-after", choices=STAGES, default="index")

    serve = commands.add_parser("serve")
    serve.add_argument("program")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        platform = load_platform(args.config)
        manifest = _program_manifest(args.programs_dir, args.program)
        program = load_program(manifest, platform)
        pipeline = Pipeline(
            platform,
            program,
            args.runs_dir,
            dry_run=getattr(args, "dry_run", False),
            force=getattr(args, "force", False),
        )
        if args.command == "doctor":
            checks = pipeline.doctor()
            for status, label, detail in checks:
                print(f"{status:7} {label}: {detail}")
            return 1 if any(status == "error" for status, _, _ in checks) else 0
        if args.command == "plan":
            print(json.dumps(pipeline.plan(), indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(pipeline.status(), indent=2))
            return 0
        if args.command == "run":
            pipeline.run(stop_after=args.stop_after)
            print(f"\nRun directory: {pipeline.run_dir}")
            return 0
        if args.command == "serve":
            pipeline.serve(args.host, args.port)
            return 0
    except (ConfigurationError, PipelineError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _program_manifest(programs_dir: Path, program: str) -> Path:
    root = programs_dir.expanduser().resolve()
    direct = root / program.upper() / "program.toml"
    if direct.is_file():
        return direct
    matches = [path for path in root.glob("*/program.toml") if path.parent.name.upper() == program.upper()]
    if matches:
        return matches[0]
    raise ConfigurationError(f"Program manifest not found: {direct}")


if __name__ == "__main__":
    raise SystemExit(main())

