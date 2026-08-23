from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path


class RektRunnerError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the team cobol-rekt analysis and export its knowledge-base_rag bundle.",
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo.expanduser().resolve()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    _preflight(repo, source)

    report_root = output.parent / "report"
    report_dir = report_root / f"{source.name}.report"
    report_root.mkdir(parents=True, exist_ok=True)

    original_cwd = Path.cwd()
    original_argv = sys.argv[:]
    original_path = sys.path[:]
    try:
        os.chdir(repo)
        sys.path.insert(0, str(repo))
        analyze = importlib.import_module("analyze")
        original_config = analyze.Config

        class ExternalOutputConfig(original_config):
            def __init__(self) -> None:
                super().__init__()
                self.report_dir = report_root

        analyze.Config = ExternalOutputConfig
        sys.argv = [
            "analyze.py",
            str(source),
            "--rag-only",
            "--lenient",
            "--no-comment-enrichment",
        ]
        analyze.main()

        chunk_pipeline = importlib.import_module("chunk_pipeline")
        summary = chunk_pipeline.run_pipeline(report_dir, verbose=args.verbose)
        if not summary:
            raise RektRunnerError(f"chunk pipeline produced no summary for {report_dir}")
    finally:
        os.chdir(original_cwd)
        sys.argv = original_argv
        sys.path[:] = original_path

    bundle = report_dir / "knowledge-base_rag"
    if not (bundle / "manifest.json").is_file():
        raise RektRunnerError(f"bundle manifest was not generated: {bundle / 'manifest.json'}")
    _replace_bundle(bundle, output)
    (output.parent / "run_summary.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "report": str(report_dir),
                "bundle": str(output),
                "chunk_count": summary.get("total", 0),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"cobol-rekt bundle: {output}")
    return 0


def _preflight(repo: Path, source: Path) -> None:
    required = (
        repo / "analyze.py",
        repo / "chunk_pipeline.py",
        repo / "smojol-cli" / "target" / "smojol-cli.jar",
        repo
        / "che-che4z-lsp-for-cobol-integration"
        / "server"
        / "dialect-idms"
        / "target"
        / "dialect-idms.jar",
        source,
    )
    missing = [path for path in required if not path.is_file()]
    if missing:
        details = "\n".join(f"  - {path}" for path in missing)
        raise RektRunnerError(
            "cobol-rekt is not built or the source is missing. Missing:\n"
            + details
            + "\nBuild with JDK 21 using: mvn clean package -Dcheckstyle.skip=true -Dmaven.test.skip=true"
        )


def _replace_bundle(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / f"{destination.name}.tmp-{uuid.uuid4().hex}"
    shutil.copytree(source, temp)
    if destination.exists():
        shutil.rmtree(destination)
    temp.replace(destination)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RektRunnerError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)

