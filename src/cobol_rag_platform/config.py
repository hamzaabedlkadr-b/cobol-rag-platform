from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised when platform or program configuration is invalid."""


@dataclass(frozen=True)
class Repositories:
    analysis: Path
    cobol_rekt: Path
    rag: Path


@dataclass(frozen=True)
class AnalysisSettings:
    python: str
    runner: Path
    mode: str
    rag_profile: str
    copy_mode: str


@dataclass(frozen=True)
class RektSettings:
    python: str
    required: bool
    command: tuple[str, ...]
    output: str


@dataclass(frozen=True)
class RagSettings:
    python: str
    collection_prefix: str
    llm_model: str
    embedding_model: str
    llm_base_url: str
    embedding_base_url: str
    ensure_models: bool


@dataclass(frozen=True)
class PlatformConfig:
    source: Path
    repositories: Repositories
    analysis: AnalysisSettings
    rekt: RektSettings
    rag: RagSettings


@dataclass(frozen=True)
class ProgramConfig:
    source: Path
    name: str
    cobol_source: Path
    copybooks: Path
    mapa: Path
    controlflow: Path
    jcl: Path | None
    rekt_bundle: Path | None


def load_platform(path: Path) -> PlatformConfig:
    path = path.expanduser().resolve()
    data = _read_toml(path)
    base = path.parent
    repos = data.get("repositories", {})
    analysis_repo = _env_path("ANALYSIS_REPO", repos.get("analysis"), base)
    rekt_repo = _env_path("COBOL_REKT_REPO", repos.get("cobol_rekt"), base)
    rag_repo = _env_path("RAG_REPO", repos.get("rag"), base)

    analysis = data.get("analysis", {})
    rekt = data.get("rekt", {})
    rag = data.get("rag", {})
    mode = str(analysis.get("mode", "auto"))
    if mode not in {"auto", "my", "combined", "both"}:
        raise ConfigurationError("analysis.mode must be auto, my, combined, or both")

    command = rekt.get("command", [])
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ConfigurationError("rekt.command must be a TOML array of command arguments")

    runner_value = Path(str(analysis.get("runner", "scripts/pipeline/run_fixed_input.py")))
    runner = runner_value if runner_value.is_absolute() else analysis_repo / runner_value
    return PlatformConfig(
        source=path,
        repositories=Repositories(analysis=analysis_repo, cobol_rekt=rekt_repo, rag=rag_repo),
        analysis=AnalysisSettings(
            python=os.getenv("ANALYSIS_PYTHON", str(analysis.get("python", "python3"))),
            runner=runner.resolve(),
            mode=mode,
            rag_profile=str(analysis.get("rag_profile", "full")),
            copy_mode=str(analysis.get("copy_mode", "referenced")),
        ),
        rekt=RektSettings(
            python=os.getenv("REKT_PYTHON", str(rekt.get("python", "python3"))),
            required=bool(rekt.get("required", False)),
            command=tuple(command),
            output=str(rekt.get("output", "{run_dir}/rekt/knowledge-base_rag")),
        ),
        rag=RagSettings(
            python=os.getenv("RAG_PYTHON", str(rag.get("python", "python3"))),
            collection_prefix=str(rag.get("collection_prefix", "cobol")),
            llm_model=os.getenv("COBOL_RAG_LLM_MODEL", str(rag.get("llm_model", "granite-code:8b-instruct"))),
            embedding_model=os.getenv(
                "COBOL_RAG_EMBEDDING_MODEL",
                str(rag.get("embedding_model", "mxbai-embed-large:latest")),
            ),
            llm_base_url=os.getenv("COBOL_RAG_LLM_BASE_URL", str(rag.get("llm_base_url", "http://localhost:11434"))),
            embedding_base_url=os.getenv(
                "COBOL_RAG_EMBEDDING_BASE_URL",
                str(rag.get("embedding_base_url", "http://localhost:11434")),
            ),
            ensure_models=bool(rag.get("ensure_models", False)),
        ),
    )


def load_program(path: Path, platform: PlatformConfig) -> ProgramConfig:
    path = path.expanduser().resolve()
    data = _read_toml(path).get("program", {})
    name = str(data.get("name", path.parent.name)).upper()
    if not name:
        raise ConfigurationError(f"Missing program.name in {path}")
    root = platform.repositories.analysis

    def required(name_: str) -> Path:
        value = data.get(name_)
        if not value:
            raise ConfigurationError(f"Missing program.{name_} in {path}")
        return _resolve_path(str(value), root)

    def optional(name_: str) -> Path | None:
        value = data.get(name_)
        return _resolve_path(str(value), root) if value else None

    return ProgramConfig(
        source=path,
        name=name,
        cobol_source=required("cobol_source"),
        copybooks=required("copybooks"),
        mapa=required("mapa"),
        controlflow=required("controlflow"),
        jcl=optional("jcl"),
        rekt_bundle=optional("rekt_bundle"),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _env_path(name: str, configured: object, base: Path) -> Path:
    raw = os.getenv(name) or (str(configured) if configured else "")
    if not raw:
        raise ConfigurationError(f"Missing repositories value and ${name}")
    return _resolve_path(raw, base)


def _resolve_path(raw: str, base: Path) -> Path:
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
