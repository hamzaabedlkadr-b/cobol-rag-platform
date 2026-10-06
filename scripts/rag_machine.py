"""Portable host-side setup/start helper for the COBOL RAG platform.

Only Python's standard library is used. The heavy analysis still runs in Docker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = {
    "control_flow": (
        "https://github.com/hamzaabedlkadr-b/legacy-program-analysis.git",
        "feature/program-capability-manifest",
    ),
    "cobol-rekt": ("https://github.com/erminlilaj/cobol-rekt.git", "feature/integration-research"),
    "cobol-rag-pipeline": ("https://github.com/erminlilaj/cobol-rag-pipeline.git", "wip/2026-09-15"),
    "cobol-rag-platform": ("https://github.com/hamzaabedlkadr-b/cobol-rag-platform.git", "wip/2026-09-15"),
}
SUBMODULES = ("che-che4z-lsp-for-cobol-integration", "mojo-common", "woof")


@dataclass(frozen=True)
class ProgramInput:
    name: str
    source: Path
    mapa: Path
    controlflow: Path


def say(message: str) -> None:
    print(message, flush=True)


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None, dry_run: bool = False) -> None:
    say("+ " + subprocess.list2cmdline(command))
    if not dry_run:
        subprocess.run(command, cwd=cwd, env=env, check=True)


def require_tool(name: str) -> None:
    if not shutil.which(name):
        raise RuntimeError(f"Required tool not found: {name}")


def check_docker() -> None:
    require_tool("docker")
    run(["docker", "info"], dry_run=False)
    run(["docker", "compose", "version"], dry_run=False)


def read_json(url: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}")
    return value


def model_default() -> str:
    if platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return "gemma4:e4b-mlx"
    return "gemma4:e4b"


def discover_programs(root: Path) -> tuple[list[ProgramInput], list[str]]:
    if not root.is_dir():
        raise RuntimeError(f"Program folder does not exist: {root}")
    complete: list[ProgramInput] = []
    skipped: list[str] = []
    names: set[str] = set()
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.upper()):
        name = folder.name.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9-]*", name):
            skipped.append(f"{folder.name}: invalid program directory name")
            continue
        if name in names:
            skipped.append(f"{folder.name}: duplicate program name (case-insensitive)")
            continue
        names.add(name)
        files = {p.name.upper(): p for p in folder.iterdir() if p.is_file()}
        expected = (f"{name}.CBL", f"{name}_RESULT.TXT", f"{name}_CONTROLFLOW.JSON")
        missing = [filename for filename in expected if filename not in files]
        if missing:
            skipped.append(f"{folder.name}: missing {', '.join(missing)}")
            continue
        complete.append(ProgramInput(name, *(files[item] for item in expected)))
    return complete, skipped


def ensure_repositories(parent: Path, *, skip_update: bool, dry_run: bool) -> None:
    require_tool("git")
    for folder, (url, branch) in REPOSITORIES.items():
        destination = parent / folder
        if not destination.exists():
            run(["git", "clone", "--branch", branch, url, str(destination)], cwd=parent, dry_run=dry_run)
            continue
        if not (destination / ".git").exists():
            raise RuntimeError(f"Existing path is not a Git checkout: {destination}")
        if skip_update:
            say(f"Keeping existing checkout: {destination}")
            continue
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=destination, text=True)
        if status.strip():
            raise RuntimeError(f"Checkout has local changes; commit/stash them or use --skip-repo-update: {destination}")
        current = subprocess.check_output(["git", "branch", "--show-current"], cwd=destination, text=True).strip()
        if current != branch:
            raise RuntimeError(f"{destination} is on {current!r}, expected {branch!r}; switch it explicitly")
        run(["git", "pull", "--ff-only", "origin", branch], cwd=destination, dry_run=dry_run)
    rekt = parent / "cobol-rekt"
    if not skip_update and (rekt.exists() or dry_run):
        run(["git", "submodule", "update", "--init", "--recursive", *SUBMODULES], cwd=rekt, dry_run=dry_run)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_checked(source: Path, target: Path, *, replace: bool, dry_run: bool) -> None:
    if target.exists() and sha256(source) != sha256(target) and not replace:
        raise RuntimeError(f"Different file already exists: {target}; use --replace-inputs to replace it")
    if dry_run:
        say(f"Would copy {source} -> {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or sha256(source) != sha256(target):
        shutil.copy2(source, target)


def write_checked(content: str, target: Path, *, replace: bool, dry_run: bool) -> None:
    if target.exists() and target.read_text() != content and not replace:
        raise RuntimeError(f"Different manifest already exists: {target}; use --replace-inputs to replace it")
    if dry_run:
        say(f"Would write {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_text() != content:
        target.write_text(content)


def manifest_matches(existing: Path, expected: str) -> bool:
    """Keep compatible manifests, including optional JCL and user comments."""
    if not existing.is_file():
        return False
    keys = ("name", "cobol_source", "copybooks", "mapa", "controlflow")

    def values(content: str) -> dict[str, str]:
        return dict(re.findall(r'^\s*(' + "|".join(keys) + r')\s*=\s*"([^"]+)"\s*$', content, re.MULTILINE))

    return values(existing.read_text()) == values(expected)


def jcl_files_in(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise RuntimeError(f"JCL folder does not exist: {folder}")
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in {".jcl", ".txt", ".proc", ".prc"})
    if not files:
        raise RuntimeError(f"No JCL/procedure files (.jcl, .txt, .proc, .prc) found in: {folder}")
    names: set[str] = set()
    for file in files:
        if file.name.casefold() in names:
            raise RuntimeError(f"Duplicate JCL filename in shared folder: {file.name}; use unique filenames")
        names.add(file.name.casefold())
    return files


def add_jcl_setting(content: str, value: str, *, replace: bool) -> str:
    # Change only the program table, retaining optional settings and comments.
    section = re.search(r"(?m)^\[program\][^\n]*\n", content)
    if not section:
        raise RuntimeError("Cannot add JCL: manifest has no [program] table")
    tail = content[section.end():]
    next_section = re.search(r"(?m)^\s*\[", tail)
    end = section.end() + (next_section.start() if next_section else len(tail))
    body = content[section.end():end]
    existing = re.search(r"(?m)^\s*jcl\s*=\s*(['\"])(.*?)\1[^\n]*", body)
    if existing:
        if existing.group(2) == value:
            return content
        if not replace:
            raise RuntimeError("Manifest points to a different JCL folder; use --replace-inputs to replace it")
        body = body[:existing.start()] + f'jcl = "{value}"' + body[existing.end():]
    else:
        body = body.rstrip("\n") + f'\njcl = "{value}"\n\n'
    return content[:section.end()] + body + content[end:]


def install_inputs(programs: list[ProgramInput], copybooks: Path, *, replace: bool, dry_run: bool,
                   jcl_dir: Path | None = None) -> None:
    if not copybooks.is_dir():
        raise RuntimeError(f"Copybook folder does not exist: {copybooks}")
    copybook_files = sorted(p for p in copybooks.rglob("*") if p.is_file())
    if not copybook_files:
        raise RuntimeError(f"Copybook folder is empty: {copybooks}")
    analysis = ROOT.parent / "control_flow" / "input"
    planned_files: list[tuple[Path, Path]] = []
    planned_manifests: list[tuple[str, Path]] = []
    jcl_files = jcl_files_in(jcl_dir) if jcl_dir is not None else []
    jcl_manifest_updates: set[Path] = set()
    for program in programs:
        target = analysis / program.name
        for source, filename in (
            (program.source, f"{program.name}.CBL"),
            (program.mapa, f"{program.name}_result.txt"),
            (program.controlflow, f"{program.name}_controlflow.json"),
        ):
            planned_files.append((source, target / filename))
        for source in copybook_files:
            planned_files.append((source, target / "copybooks" / source.relative_to(copybooks)))
        # The analyzer scans this directory non-recursively. Flatten only after
        # validating unique filenames so nested uploads cannot overwrite each other.
        for source in jcl_files:
            planned_files.append((source, target / "jcl" / source.name))
        manifest = (
            "[program]\n"
            f'name = "{program.name}"\n'
            f'cobol_source = "input/{program.name}/{program.name}.CBL"\n'
            f'copybooks = "input/{program.name}/copybooks"\n'
            f'mapa = "input/{program.name}/{program.name}_result.txt"\n'
            f'controlflow = "input/{program.name}/{program.name}_controlflow.json"\n'
        )
        manifest_path = ROOT / "programs" / program.name / "program.toml"
        if manifest_matches(manifest_path, manifest):
            if jcl_dir is not None:
                original = manifest_path.read_text()
                updated = add_jcl_setting(original, f"input/{program.name}/jcl", replace=replace)
                if updated != original:
                    planned_manifests.append((updated, manifest_path))
                    jcl_manifest_updates.add(manifest_path)
            else:
                say(f"Keeping compatible manifest: {manifest_path}")
        else:
            if jcl_dir is not None:
                manifest += f'jcl = "input/{program.name}/jcl"\n'
            planned_manifests.append((manifest, manifest_path))
    # Check every collision before copying anything, so a conflict does not leave
    # a partially imported program behind.
    if not replace:
        for source, target in planned_files:
            if target.exists() and sha256(source) != sha256(target):
                raise RuntimeError(f"Different file already exists: {target}; use --replace-inputs to replace it")
        for content, target in planned_manifests:
            if target.exists() and target.read_text() != content and target not in jcl_manifest_updates:
                raise RuntimeError(f"Different manifest already exists: {target}; use --replace-inputs to replace it")
    for source, target in planned_files:
        copy_checked(source, target, replace=replace, dry_run=dry_run)
    for content, target in planned_manifests:
        write_checked(content, target, replace=replace or target in jcl_manifest_updates, dry_run=dry_run)


def update_env(settings: dict[str, str], *, dry_run: bool) -> None:
    path = ROOT / ".env"
    original = path.read_text() if path.exists() else (ROOT / ".env.example").read_text()
    lines = original.splitlines()
    pending = dict(settings)
    result = []
    for line in lines:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            result.append(f"{key}={pending.pop(key)}")
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in pending.items())
    if dry_run:
        say(f"Would update {path} (preserving unrelated settings)")
    else:
        path.write_text("\n".join(result) + "\n")


def wait_api(expected: set[str], *, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health = read_json("http://localhost:8000/api/health", timeout=5)
            if health.get("status") != "ok":
                raise RuntimeError(f"Unexpected health response: {health}")
            info = read_json("http://localhost:8000/api/index-info", timeout=15)
            if not isinstance(info.get("documents"), int) or info["documents"] <= 0:
                raise RuntimeError("Index has no documents")
            say("Health: " + json.dumps(health, sort_keys=True))
            say("Index info: " + json.dumps(info, sort_keys=True))
            registry_path = ROOT / ".runs" / "_corpus" / "rag" / "final_scripts" / "corpus.registry.json"
            registry = json.loads(registry_path.read_text())
            indexed = {str(item["program"]).upper() for item in registry.get("programs", []) if isinstance(item, dict) and item.get("program")}
            if not expected.issubset(indexed):
                raise RuntimeError(f"Missing indexed programs: {', '.join(sorted(expected - indexed))}")
            say("Indexed programs: " + ", ".join(sorted(indexed)))
            return
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(2)
    raise RuntimeError(f"API/index did not become ready within {timeout}s: {last_error}")


def pull_models(url: str, models: tuple[str, str], *, dry_run: bool) -> None:
    require_tool("ollama")
    if not dry_run:
        read_json(url.rstrip("/") + "/api/version")
    parsed = urlparse(url)
    env = dict(os.environ)
    env["OLLAMA_HOST"] = parsed.netloc
    for model in dict.fromkeys(models):
        run(["ollama", "pull", model], env=env, dry_run=dry_run)


def setup(args: argparse.Namespace) -> None:
    programs, skipped = discover_programs(args.programs_dir.resolve())
    for reason in skipped:
        say("SKIP " + reason)
    if not programs:
        raise RuntimeError("No complete program folders found; nothing will be indexed")
    if not args.copybooks_dir.is_dir() or not any(p.is_file() for p in args.copybooks_dir.rglob("*")):
        raise RuntimeError(f"Copybook folder is missing or empty: {args.copybooks_dir}")
    if args.jcl_dir is not None:
        say(f"JCL files available: {len(jcl_files_in(args.jcl_dir))}")
    say("Programs to index: " + ", ".join(item.name for item in programs))
    if not args.dry_run:
        check_docker()
    ensure_repositories(ROOT.parent, skip_update=args.skip_repo_update, dry_run=args.dry_run)
    if not args.skip_model_pull:
        pull_models(args.ollama_url, (args.llm_model, args.embedding_model), dry_run=args.dry_run)
    else:
        say("Model pull skipped; ensure both models exist on the configured Ollama server")
    install_inputs(programs, args.copybooks_dir.resolve(), replace=args.replace_inputs, dry_run=args.dry_run,
                   jcl_dir=args.jcl_dir.resolve() if args.jcl_dir is not None else None)
    container_url = args.container_ollama_url or (
        "http://host.docker.internal:11434" if urlparse(args.ollama_url).hostname in {"localhost", "127.0.0.1"}
        else args.ollama_url
    )
    update_env({
        "ANALYSIS_REPO": "../control_flow",
        "RAG_REPO": "../cobol-rag-pipeline",
        "COBOL_REKT_REPO": "../cobol-rekt",
        "COBOL_RAG_LLM_MODEL": args.llm_model,
        "COBOL_RAG_EMBEDDING_MODEL": args.embedding_model,
        "COBOL_RAG_LLM_BASE_URL": container_url,
        "COBOL_RAG_EMBEDDING_BASE_URL": container_url,
        "COBOL_RAG_INVESTIGATION": "1",
        "COBOL_RAG_MEMORY_ENABLED": "false",
    }, dry_run=args.dry_run)
    run(["docker", "compose", "build", "pipeline", "rag-api"], dry_run=args.dry_run)
    for program in programs:
        for action in ("doctor", "run"):
            run(["docker", "compose", "run", "--rm", "pipeline", action, program.name], dry_run=args.dry_run)
    run(["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "rag-api"], dry_run=args.dry_run)
    if not args.dry_run:
        wait_api({item.name for item in programs})
    say("Open http://localhost:8000")


def start(args: argparse.Namespace) -> None:
    check_docker()
    ollama_url = args.ollama_url
    if not ollama_url:
        env_path = ROOT / ".env"
        if env_path.is_file():
            match = re.search(r"^COBOL_RAG_LLM_BASE_URL=(.+)$", env_path.read_text(), re.MULTILINE)
            if match:
                ollama_url = match.group(1).strip().replace("host.docker.internal", "localhost")
    health_url = (ollama_url or "http://localhost:11434").rstrip("/") + "/api/version"
    try:
        read_json(health_url)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f"Ollama is not reachable at {health_url}: {error}") from error
    run(["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "rag-api"])
    wait_api(set())
    say("Open http://localhost:8000")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("setup", help="Import complete program bundles and build the corpus")
    install.add_argument("--programs-dir", type=Path, required=True, help="Parent containing one folder per program")
    install.add_argument("--copybooks-dir", type=Path, required=True, help="Shared copybook tree")
    install.add_argument("--jcl-dir", type=Path, help="Optional shared folder of .jcl/.txt jobs and .proc/.prc procedure members (unique filenames)")
    install.add_argument("--llm-model", default=model_default())
    install.add_argument("--embedding-model", default="mxbai-embed-large:latest")
    install.add_argument("--ollama-url", default="http://localhost:11434", help="URL reachable from host")
    install.add_argument("--container-ollama-url", help="URL reachable from Docker; defaults to host gateway")
    install.add_argument("--skip-repo-update", action="store_true")
    install.add_argument("--skip-model-pull", action="store_true")
    install.add_argument("--replace-inputs", action="store_true", help="Allow replacing different imported files/manifests")
    install.add_argument("--dry-run", action="store_true")
    launch = sub.add_parser("start", help="Recreate the API without rebuilding the corpus")
    launch.add_argument("--ollama-url", help="Host-reachable Ollama URL; defaults to value inferred from .env")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(args)
        else:
            start(args)
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
