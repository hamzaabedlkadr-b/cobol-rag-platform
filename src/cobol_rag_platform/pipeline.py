from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cobol_rag_platform.config import PlatformConfig, ProgramConfig


STAGES = ("prepare", "rekt", "analysis", "models", "index")


class PipelineError(RuntimeError):
    """Raised for an actionable pipeline failure."""


@dataclass(frozen=True)
class StageResult:
    name: str
    status: str
    fingerprint: str
    outputs: tuple[str, ...] = ()
    note: str = ""


class Pipeline:
    def __init__(
        self,
        platform: PlatformConfig,
        program: ProgramConfig,
        runs_root: Path,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> None:
        self.platform = platform
        self.program = program
        self.runs_root = runs_root.expanduser().resolve()
        self.run_dir = self.runs_root / program.name
        self.dry_run = dry_run
        self.force = force
        self.state_path = self.run_dir / "state.json"
        self.state = self._read_state()

    @property
    def prepared_root(self) -> Path:
        return self.run_dir / "work" / "input"

    @property
    def analysis_output(self) -> Path:
        return self.run_dir / "analysis" / "output"

    @property
    def rag_runtime(self) -> Path:
        # Analysis state remains program-specific, but all programs publish into
        # one searchable corpus so an explicit program name can be enforced at
        # retrieval time.
        return self.runs_root / "_corpus" / "rag"

    @property
    def legacy_rag_runtime(self) -> Path:
        return self.run_dir / "rag"

    @property
    def corpus_final_scripts(self) -> Path:
        return self.rag_runtime / "final_scripts"

    def run(self, stop_after: str = "index") -> list[StageResult]:
        if stop_after not in STAGES:
            raise PipelineError(f"Unknown stop stage: {stop_after}")
        results: list[StageResult] = []
        for stage in STAGES:
            method = getattr(self, f"stage_{stage}")
            print(f"\n== {stage} ==", flush=True)
            try:
                result = method()
            except Exception as error:
                if not self.dry_run:
                    self._record_failure(stage, error)
                raise
            results.append(result)
            self._print_result(result)
            if stage == stop_after:
                break
        return results

    def plan(self) -> list[dict[str, Any]]:
        bundle = self._bundle_for_planning()
        mode = self._analysis_mode(bundle)
        artifact, final_scripts = self._rag_source(mode)
        return [
            {"stage": "prepare", "input": str(self.program.source), "output": str(self.prepared_root)},
            {
                "stage": "rekt",
                "status": "prebuilt" if bundle else ("configured" if self.platform.rekt.command else "optional-not-configured"),
                "bundle": str(bundle) if bundle else None,
            },
            {"stage": "analysis", "mode": mode, "command": self._analysis_command(bundle, mode)},
            {
                "stage": "models",
                "status": "ensure" if self.platform.rag.ensure_models else "external-prerequisite",
                "models": [self.platform.rag.llm_model, self.platform.rag.embedding_model],
            },
            {
                "stage": "index",
                "artifact": str(artifact),
                "final_scripts": str(final_scripts),
                "collection": self.collection,
            },
        ]

    def doctor(self) -> list[tuple[str, str, str]]:
        checks: list[tuple[str, str, str]] = []

        def exists(label: str, path: Path, kind: str) -> None:
            valid = path.is_dir() if kind == "directory" else path.is_file()
            checks.append(("ok" if valid else "error", label, str(path)))

        exists("analysis repository", self.platform.repositories.analysis, "directory")
        exists("analysis runner", self.platform.analysis.runner, "file")
        exists("RAG repository", self.platform.repositories.rag, "directory")
        exists("RAG package", self.platform.repositories.rag / "src" / "cobol_rag" / "cli.py", "file")
        rekt_entries = (
            [item for item in self.platform.repositories.cobol_rekt.iterdir() if item.name != ".gitkeep"]
            if self.platform.repositories.cobol_rekt.is_dir()
            else []
        )
        if rekt_entries:
            checks.append(("ok", "cobol-rekt repository", str(self.platform.repositories.cobol_rekt)))
            rekt_jar = self.platform.repositories.cobol_rekt / "smojol-cli" / "target" / "smojol-cli.jar"
            dialect_jar = (
                self.platform.repositories.cobol_rekt
                / "che-che4z-lsp-for-cobol-integration"
                / "server"
                / "dialect-idms"
                / "target"
                / "dialect-idms.jar"
            )
            if not rekt_jar.is_file() or not dialect_jar.is_file():
                checks.append(
                    ("warning", "cobol-rekt build", "JARs missing on host; the Docker image builds them automatically")
                )
        elif self.program.rekt_bundle and self._normalize_bundle(self.program.rekt_bundle):
            checks.append(("ok", "cobol-rekt evidence", str(self.program.rekt_bundle)))
        else:
            level = "error" if self.platform.rekt.required else "warning"
            checks.append((level, "cobol-rekt", "repository/bundle not present; MAPA-only mode remains available"))
        exists("COBOL source", self.program.cobol_source, "file")
        exists("copybooks", self.program.copybooks, "directory")
        exists("MAPA result", self.program.mapa, "file")
        exists("control-flow", self.program.controlflow, "file")
        if sys.version_info < (3, 11):
            checks.append(("error", "Python", "Python 3.11 or newer is required"))
        else:
            checks.append(("ok", "Python", sys.version.split()[0]))
        return checks

    @property
    def collection(self) -> str:
        return f"{self.platform.rag.collection_prefix}-corpus"

    @property
    def legacy_collection(self) -> str:
        return f"{self.platform.rag.collection_prefix}-{self.program.name.lower()}"

    def stage_prepare(self) -> StageResult:
        inputs = [self.program.source, self.program.cobol_source, self.program.copybooks, self.program.mapa, self.program.controlflow]
        if self.program.jcl:
            inputs.append(self.program.jcl)
        fingerprint = fingerprint_paths(inputs, extra={"program": self.program.name})
        target = self.prepared_root / self.program.name
        if self._cached("prepare", fingerprint, (target,)):
            return StageResult("prepare", "cached", fingerprint, (str(target),))
        if self.dry_run:
            return StageResult("prepare", "planned", fingerprint, (str(target),))

        temp = self.run_dir / "work" / f"input.tmp-{uuid.uuid4().hex}"
        program_dir = temp / self.program.name
        program_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.program.cobol_source, program_dir / self.program.cobol_source.name)
        shutil.copy2(self.program.mapa, program_dir / self.program.mapa.name)
        shutil.copy2(self.program.controlflow, program_dir / self.program.controlflow.name)
        shutil.copytree(self.program.copybooks, program_dir / "copybooks")
        if self.program.jcl:
            shutil.copytree(self.program.jcl, program_dir / "jcl")
        self.prepared_root.parent.mkdir(parents=True, exist_ok=True)
        self._replace_generated_dir(temp, self.prepared_root)
        return self._complete("prepare", fingerprint, (target,))

    def stage_rekt(self) -> StageResult:
        prebuilt = self._normalize_bundle(self.program.rekt_bundle) if self.program.rekt_bundle else None
        configured_output = Path(self._format(self.platform.rekt.output))
        generated = self._normalize_bundle(configured_output)
        extra = {"command": list(self.platform.rekt.command), "output": self.platform.rekt.output}
        inputs: list[Path] = [
            self.program.cobol_source,
            self.program.copybooks,
            self.platform.repositories.cobol_rekt / "analyze.py",
            self.platform.repositories.cobol_rekt / "chunk_pipeline.py",
            self.platform.repositories.cobol_rekt / "smojol-cli" / "target" / "smojol-cli.jar",
            self.platform.repositories.cobol_rekt
            / "che-che4z-lsp-for-cobol-integration"
            / "server"
            / "dialect-idms"
            / "target"
            / "dialect-idms.jar",
        ]
        if prebuilt:
            inputs.append(prebuilt)
        fingerprint = fingerprint_paths(inputs, extra=extra)
        if prebuilt:
            return self._complete("rekt", fingerprint, (prebuilt,), note="using configured knowledge-base_rag bundle")
        if generated and self._cached("rekt", fingerprint, (generated,)):
            return StageResult("rekt", "cached", fingerprint, (str(generated),))
        if not self.platform.rekt.command:
            if self.platform.rekt.required:
                raise PipelineError(
                    "cobol-rekt is required, but no bundle exists and rekt.command is not configured in platform.toml"
                )
            return StageResult("rekt", "skipped", fingerprint, note="no exporter configured; continuing with MAPA/Hamza")

        output = configured_output
        command = [self._format(token, rekt_output=output) for token in self.platform.rekt.command]
        if self._cached("rekt", fingerprint, (output,)):
            return StageResult("rekt", "cached", fingerprint, (str(output),))
        self._run_command("cobol-rekt", command, cwd=self.platform.repositories.cobol_rekt)
        bundle = self._normalize_bundle(output)
        if not self.dry_run and bundle is None:
            raise PipelineError(f"cobol-rekt command completed but no bundle manifest was found under {output}")
        resolved_output = bundle or output
        return self._complete("rekt", fingerprint, (resolved_output,)) if not self.dry_run else StageResult(
            "rekt", "planned", fingerprint, (str(output),)
        )

    def stage_analysis(self) -> StageResult:
        bundle = self._bundle_for_planning() if self.dry_run else self._resolve_bundle(allow_missing=True)
        mode = self._analysis_mode(bundle)
        runner_inputs = [
            self.prepared_root,
            self.platform.analysis.runner.parent,
            self.platform.repositories.analysis / "artifacts" / "final" / "final_scripts",
            self.program.source,
        ]
        if bundle:
            runner_inputs.append(bundle)
        fingerprint = fingerprint_paths(
            runner_inputs,
            extra={"mode": mode, "rag_profile": self.platform.analysis.rag_profile, "copy_mode": self.platform.analysis.copy_mode},
        )
        artifact, _ = self._rag_source(mode)
        if self._cached("analysis", fingerprint, (artifact,)):
            return StageResult("analysis", "cached", fingerprint, (str(artifact),), note=f"mode={mode}")
        command = self._analysis_command(bundle, mode)
        self._run_command("analysis", command, cwd=self.platform.repositories.analysis)
        if not self.dry_run and not artifact.is_file():
            raise PipelineError(f"analysis completed but expected RAG artifact is missing: {artifact}")
        return self._complete("analysis", fingerprint, (artifact,), note=f"mode={mode}") if not self.dry_run else StageResult(
            "analysis", "planned", fingerprint, (str(artifact),), note=f"mode={mode}"
        )

    def stage_models(self) -> StageResult:
        models = (self.platform.rag.llm_model, self.platform.rag.embedding_model)
        fingerprint = fingerprint_paths([], extra={"models": models, "base_url": self.platform.rag.llm_base_url})
        if not self.platform.rag.ensure_models:
            return StageResult("models", "skipped", fingerprint, note="model lifecycle is externally managed")
        if self.dry_run:
            return StageResult("models", "planned", fingerprint, models)
        for model in models:
            self._ensure_ollama_model(model)
        return self._complete("models", fingerprint, (), note="Ollama models available")

    def stage_index(self) -> StageResult:
        bundle = self._bundle_for_planning() if self.dry_run else self._resolve_bundle(allow_missing=True)
        mode = self._analysis_mode(bundle)
        artifact, final_scripts = self._rag_source(mode)
        inputs = [artifact, self.platform.repositories.rag / "src", self.platform.repositories.rag / "config" / "system_prompt.md"]
        fingerprint = fingerprint_paths(
            inputs,
            extra={
                "collection": self.collection,
                "llm_model": self.platform.rag.llm_model,
                "embedding_model": self.platform.rag.embedding_model,
            },
        )
        manifest = self.rag_runtime / "data" / "manifests" / f"{self.collection}.json"
        chroma = self.rag_runtime / ".chroma"
        if self._cached("index", fingerprint, (manifest, chroma)):
            return StageResult("index", "cached", fingerprint, (str(chroma), str(manifest)))
        if not artifact.is_file() and not self.dry_run:
            raise PipelineError(f"RAG artifact does not exist: {artifact}")
        config_path = self.rag_runtime / "config" / "runtime.yaml"
        if not self.dry_run:
            self._publish_program_artifacts(final_scripts)
            self._write_rag_runtime(config_path, self.corpus_final_scripts)
        command = [
            self.platform.rag.python,
            "-m",
            "cobol_rag.cli",
            "sync",
            str(artifact),
            "--apply",
            "--config",
            str(config_path),
        ]
        env = self._rag_environment(self.corpus_final_scripts)
        self._run_command("RAG index", command, cwd=self.platform.repositories.rag, env=env)
        if not self.dry_run and not manifest.is_file():
            raise PipelineError(f"RAG sync completed but collection manifest is missing: {manifest}")
        return self._complete("index", fingerprint, (chroma, manifest)) if not self.dry_run else StageResult(
            "index", "planned", fingerprint, (str(chroma), str(manifest))
        )

    def status(self) -> dict[str, Any]:
        return self.state

    def serve(self, host: str, port: int) -> None:
        mode = self._analysis_mode(self._resolve_bundle(allow_missing=True))
        _, program_final_scripts = self._rag_source(mode)
        runtime = self.rag_runtime
        final_scripts = self.corpus_final_scripts
        collection = self.collection
        manifest = runtime / "data" / "manifests" / f"{collection}.json"
        if not manifest.is_file():
            # Preserve existing single-program deployments until they are indexed
            # once with the shared-corpus runtime.
            runtime = self.legacy_rag_runtime
            final_scripts = program_final_scripts
            collection = self.legacy_collection
            manifest = runtime / "data" / "manifests" / f"{collection}.json"
        if not manifest.is_file():
            raise PipelineError(f"Collection is not indexed. Run the pipeline first: {manifest}")
        self._prepare_prompt(runtime)
        command = [
            self.platform.rag.python,
            "-m",
            "uvicorn",
            "cobol_rag.api:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
        self._run_command(
            "RAG API",
            command,
            cwd=runtime,
            env=self._rag_environment(final_scripts, runtime=runtime, collection=collection),
        )

    def _analysis_command(self, bundle: Path | None, mode: str) -> list[str]:
        command = [
            self.platform.analysis.python,
            str(self.platform.analysis.runner),
            "--program",
            self.program.name,
            "--input-root",
            str(self.prepared_root),
            "--output-root",
            str(self.analysis_output),
            "--mode",
            mode,
            "--rag-profile",
            self.platform.analysis.rag_profile,
            "--copy-mode",
            self.platform.analysis.copy_mode,
            "--no-clean",
        ]
        if bundle and mode in {"combined", "both"}:
            command.extend(
                [
                    "--cobol-rekt-rag-bundle",
                    str(bundle),
                    "--source-label",
                    f"cobol-rekt/knowledge-base_rag/{self.program.name}",
                ]
            )
        return command

    def _analysis_mode(self, bundle: Path | None) -> str:
        configured = self.platform.analysis.mode
        if configured == "auto":
            return "both" if bundle else "my"
        if configured in {"combined", "both"} and bundle is None:
            raise PipelineError(f"analysis.mode={configured} requires a knowledge-base_rag bundle")
        return configured

    def _rag_source(self, mode: str) -> tuple[Path, Path]:
        if mode in {"combined", "both"}:
            return (
                self.analysis_output / "combined" / "rag_index" / f"{self.program.name}_combined.jsonl",
                self.analysis_output / "combined" / "final_scripts" / self.program.name,
            )
        return (
            self.analysis_output / "rag_index" / "rag_documents.jsonl",
            self.analysis_output / "program_artifacts" / "programs" / self.program.name / "artifacts",
        )

    def _resolve_bundle(self, allow_missing: bool) -> Path | None:
        candidates: list[Path] = []
        if self.program.rekt_bundle:
            candidates.append(self.program.rekt_bundle)
        candidates.append(Path(self._format(self.platform.rekt.output)))
        for candidate in candidates:
            bundle = self._normalize_bundle(candidate)
            if bundle:
                return bundle
        if allow_missing:
            return None
        raise PipelineError("No knowledge-base_rag bundle found")

    def _bundle_for_planning(self) -> Path | None:
        return self._resolve_bundle(allow_missing=True) or (
            Path(self._format(self.platform.rekt.output)) if self.platform.rekt.command else None
        )

    @staticmethod
    def _normalize_bundle(path: Path) -> Path | None:
        for candidate in (path, path / "knowledge-base_rag"):
            if (candidate / "manifest.json").is_file():
                return candidate.resolve()
        return None

    def _format(self, value: str, rekt_output: Path | None = None) -> str:
        values = {
            "program": self.program.name,
            "source": str(self.program.cobol_source),
            "copybooks": str(self.program.copybooks),
            "mapa": str(self.program.mapa),
            "controlflow": str(self.program.controlflow),
            "run_dir": str(self.run_dir),
            "prepared_program_dir": str(self.prepared_root / self.program.name),
            "rekt_output": str(rekt_output or ""),
            "analysis_output": str(self.analysis_output),
            "rekt_python": self.platform.rekt.python,
            "rekt_repo": str(self.platform.repositories.cobol_rekt),
            "source_name": self.program.cobol_source.name,
        }
        try:
            return value.format_map(values)
        except KeyError as error:
            raise PipelineError(f"Unknown command placeholder: {error.args[0]}") from error

    def _run_command(
        self,
        label: str,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> None:
        print("+ " + subprocess.list2cmdline(command), flush=True)
        if self.dry_run:
            return
        if not cwd.is_dir():
            raise PipelineError(f"{label} working directory does not exist: {cwd}")
        try:
            subprocess.run(command, cwd=cwd, env=env, check=True)
        except FileNotFoundError as error:
            raise PipelineError(f"{label} executable was not found: {command[0]}") from error
        except subprocess.CalledProcessError as error:
            raise PipelineError(f"{label} failed with exit code {error.returncode}") from error

    def _write_rag_runtime(self, path: Path, final_scripts: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        runtime = self.rag_runtime
        (runtime / "data" / "inbox").mkdir(parents=True, exist_ok=True)
        (runtime / "data" / "archive").mkdir(parents=True, exist_ok=True)
        (runtime / "data" / "manifests").mkdir(parents=True, exist_ok=True)
        (runtime / "data" / "traces").mkdir(parents=True, exist_ok=True)
        (runtime / "data" / "feedback").mkdir(parents=True, exist_ok=True)
        (runtime / "data" / "eval").mkdir(parents=True, exist_ok=True)
        self._prepare_prompt(runtime)
        payload = f'''paths:
  chroma_dir: "{runtime / '.chroma'}"
  inbox_dir: "{runtime / 'data' / 'inbox'}"
  archive_dir: "{runtime / 'data' / 'archive'}"
  manifest_dir: "{runtime / 'data' / 'manifests'}"
  trace_dir: "{runtime / 'data' / 'traces'}"
  feedback_dir: "{runtime / 'data' / 'feedback'}"
  eval_dir: "{runtime / 'data' / 'eval'}"

llm:
  provider: "ollama"
  model: "{self.platform.rag.llm_model}"
  base_url: "{self.platform.rag.llm_base_url}"
  context_window: 4096
  request_timeout: 300
  temperature: 0.1
  max_output_tokens: 256

embedding:
  provider: "ollama"
  model: "{self.platform.rag.embedding_model}"
  base_url: "{self.platform.rag.embedding_base_url}"

index:
  collection: "{self.collection}"
  chunk_mode: "pre_chunked"
  batch_size: 64
  include_non_indexable: false

retrieval:
  top_k: 6
  filters: {{}}
  similarity_cutoff: null
  mode: "hybrid"
  bm25_top_k: 12

answers:
  require_citations: true
  show_sources: true
  system_prompt_path: "{runtime / 'config' / 'system_prompt.md'}"
  max_context_chars: 6000

observability:
  enabled: true
  include_hit_previews: false
'''
        path.write_text(payload, encoding="utf-8")
        (runtime / "runtime.json").write_text(
            json.dumps(
                {
                    "program": self.program.name,
                    "collection": self.collection,
                    "final_scripts": str(final_scripts),
                    "config": str(path),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _prepare_prompt(self, runtime: Path | None = None) -> None:
        runtime = runtime or self.rag_runtime
        destination = runtime / "config" / "system_prompt.md"
        source = self.platform.repositories.rag / "config" / "system_prompt.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, destination)
        elif not destination.exists():
            destination.write_text("Answer only from retrieved COBOL evidence and cite the supporting sources.\n", encoding="utf-8")

    def _rag_environment(
        self,
        final_scripts: Path,
        *,
        runtime: Path | None = None,
        collection: str | None = None,
    ) -> dict[str, str]:
        runtime = runtime or self.rag_runtime
        collection = collection or self.collection
        env = os.environ.copy()
        source_root = str(self.platform.repositories.rag / "src")
        env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.update(
            {
                "COBOL_RAG_CHROMA_DIR": str(runtime / ".chroma"),
                "COBOL_RAG_INBOX_DIR": str(runtime / "data" / "inbox"),
                "COBOL_RAG_COLLECTION": collection,
                "COBOL_RAG_LLM_MODEL": self.platform.rag.llm_model,
                "COBOL_RAG_LLM_BASE_URL": self.platform.rag.llm_base_url,
                "COBOL_RAG_EMBEDDING_MODEL": self.platform.rag.embedding_model,
                "COBOL_RAG_EMBEDDING_BASE_URL": self.platform.rag.embedding_base_url,
                "COBOL_RAG_FINAL_SCRIPTS_DIR": str(final_scripts),
            }
        )
        return env

    def _publish_program_artifacts(self, source: Path) -> None:
        """Atomically publish one program into the shared final_scripts registry."""
        if not source.is_dir():
            raise PipelineError(f"Program final_scripts directory does not exist: {source}")
        destination = self.corpus_final_scripts / self.program.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.parent / f".{self.program.name}.tmp-{uuid.uuid4().hex}"
        shutil.copytree(source, temp)
        if destination.parent.resolve() != self.corpus_final_scripts.resolve():
            raise PipelineError(f"Refusing to publish outside corpus registry: {destination}")
        if destination.exists():
            shutil.rmtree(destination)
        temp.replace(destination)
        self._write_corpus_registry()

    def _write_corpus_registry(self) -> None:
        """Write the small routing catalogue used before any retrieval occurs."""
        programs: list[dict[str, Any]] = []
        if self.corpus_final_scripts.is_dir():
            for program_root in sorted(path for path in self.corpus_final_scripts.iterdir() if path.is_dir()):
                program = program_root.name.upper()
                entities: dict[tuple[str, str], dict[str, str]] = {}

                def add(entity_type: str, value: Any, entity_key: str | None = None) -> None:
                    normalized = str(value or "").strip().upper()
                    if not normalized:
                        return
                    key = entity_key or f"{program}|{entity_type.upper()}|{normalized}"
                    entities[(entity_type, normalized)] = {
                        "type": entity_type, "value": normalized, "entity_key": key,
                    }

                for path in program_root.rglob("dataflow.variable.*.json"):
                    add("variable", path.name[len("dataflow.variable.") : -5])

                call_path = next(iter(program_root.rglob("architecture.call_parameters.json")), None)
                calls = self._read_json_file(call_path).get("calls", []) if call_path else []
                for call in calls if isinstance(calls, list) else []:
                    if not isinstance(call, dict):
                        continue
                    target = str(call.get("target", "")).upper()
                    call_type = str(call.get("call_type", "CALL")).upper()
                    add("call", target, f"{program}|{target}|{call_type}")

                copybook_path = next(iter(program_root.rglob("architecture.copybooks.json")), None)
                copybook_content = self._read_json_file(copybook_path).get("content", {}) if copybook_path else {}
                for name in copybook_content.get("all", []) if isinstance(copybook_content, dict) else []:
                    add("copybook", name)

                cfg_path = next(iter(program_root.rglob("controlflow.cfg.json")), None)
                nodes = self._read_json_file(cfg_path).get("nodes", []) if cfg_path else []
                for node in nodes if isinstance(nodes, list) else []:
                    add("paragraph", node.get("id") if isinstance(node, dict) else node)

                programs.append({
                    "program": program,
                    "artifact_root": str(program_root),
                    "entities": sorted(entities.values(), key=lambda item: (item["type"], item["value"])),
                })

        registry = {
            "schema_version": 1,
            "program_count": len(programs),
            "programs": programs,
        }
        path = self.corpus_final_scripts / "corpus.registry.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _read_json_file(path: Path | None) -> dict[str, Any]:
        if path is None or not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _ensure_ollama_model(self, model: str) -> None:
        base_url = self.platform.rag.llm_base_url.rstrip("/")
        try:
            with urllib.request.urlopen(base_url + "/api/tags", timeout=10) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise PipelineError(f"Ollama is not reachable at {base_url}") from error
        names = {item.get("name") for item in payload.get("models", [])}
        aliases = {name.split(":", 1)[0] for name in names if isinstance(name, str)}
        if model in names or (":" not in model and model in aliases):
            print(f"model available: {model}", flush=True)
            return
        print(f"pulling Ollama model: {model}", flush=True)
        request = urllib.request.Request(
            base_url + "/api/pull",
            data=json.dumps({"name": model, "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3600) as response:
                result = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise PipelineError(f"Failed to pull Ollama model: {model}") from error
        if result.get("status") != "success":
            raise PipelineError(f"Ollama did not confirm model pull: {model}: {result}")

    def _cached(self, stage: str, fingerprint: str, outputs: Iterable[Path]) -> bool:
        if self.force:
            return False
        previous = self.state.get("stages", {}).get(stage, {})
        return (
            previous.get("status") == "complete"
            and previous.get("fingerprint") == fingerprint
            and all(path.exists() for path in outputs)
        )

    def _complete(self, stage: str, fingerprint: str, outputs: Iterable[Path], note: str = "") -> StageResult:
        result = StageResult(stage, "complete", fingerprint, tuple(str(path) for path in outputs), note)
        self.state.setdefault("stages", {})[stage] = {
            "status": "complete",
            "fingerprint": fingerprint,
            "outputs": list(result.outputs),
            "note": note,
            "finished_at": _now(),
        }
        self.state.update({"version": 1, "program": self.program.name, "updated_at": _now()})
        self._write_state()
        return result

    def _record_failure(self, stage: str, error: Exception) -> None:
        self.state.setdefault("stages", {})[stage] = {
            "status": "failed",
            "error": str(error),
            "finished_at": _now(),
        }
        self.state.update({"version": 1, "program": self.program.name, "updated_at": _now()})
        self._write_state()

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"version": 1, "program": self.program.name, "stages": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "program": self.program.name, "stages": {}}

    def _write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.state_path)

    def _replace_generated_dir(self, source: Path, destination: Path) -> None:
        if destination.exists():
            if self.run_dir not in destination.parents:
                raise PipelineError(f"Refusing to replace directory outside run root: {destination}")
            shutil.rmtree(destination)
        source.replace(destination)

    @staticmethod
    def _print_result(result: StageResult) -> None:
        detail = f" ({result.note})" if result.note else ""
        print(f"{result.status}{detail}", flush=True)
        for output in result.outputs:
            print(f"  {output}", flush=True)


def fingerprint_paths(paths: Iterable[Path], extra: object | None = None) -> str:
    digest = hashlib.sha256()
    if extra is not None:
        digest.update(json.dumps(extra, sort_keys=True, default=str).encode("utf-8"))
    for path in sorted({item.expanduser().resolve() for item in paths}, key=str):
        digest.update(str(path).encode("utf-8"))
        if not path.exists():
            digest.update(b"<missing>")
        elif path.is_file():
            _hash_file(digest, path)
        elif path.is_dir():
            for child in sorted((item for item in path.rglob("*") if item.is_file()), key=str):
                relative = child.relative_to(path)
                if any(part in {".git", "__pycache__", ".venv"} for part in relative.parts):
                    continue
                digest.update(str(relative).encode("utf-8"))
                _hash_file(digest, child)
    return digest.hexdigest()


def _hash_file(digest: Any, path: Path) -> None:
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
