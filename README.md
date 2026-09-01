# COBOL RAG Platform

This repository is a thin orchestration layer. It keeps the three codebases independent while giving them one input contract, one command, isolated outputs, and content-based stage caching.

## Pipeline

```text
program.toml
    |
    +--> prepare and validate COBOL, copybooks, MAPA, and control-flow
    |
    +--> optional cobol-rekt exporter --> knowledge-base_rag/
    |
    +--> MAPA/Hamza fixed-input pipeline --> RAG JSONL
    |                                      or combined RAG JSONL
    +--> Ollama embeddings --> Chroma collection --> RAG API/UI
```

`analysis.mode = "auto"` selects `both` when a valid `knowledge-base_rag/manifest.json` is present and `my` otherwise. It never silently pretends that cobol-rekt ran.

## Repositories

The default sibling layout is:

```text
Camera/
  control_flow/             # legacy-program-analysis / MAPA-Hamza pipeline
  cobol-rekt/               # second analyzer and RAG evidence exporter
  cobol-rag-pipeline/       # Chroma + retrieval + answer API
  cobol-rag-platform/       # this repository
```

The repositories and integration branches are:

- Analysis: https://github.com/hamzaabedlkadr-b/legacy-program-analysis (`feature/program-capability-manifest`)
- cobol-rekt: https://github.com/erminlilaj/cobol-rekt (`feature/integration-research`)
- RAG/UI: https://github.com/erminlilaj/cobol-rag-pipeline (`feature/semantic-capability-routing`)
- Platform: https://github.com/hamzaabedlkadr-b/cobol-rag-platform (`feature/map-entity-registry`)

The platform invokes the team cobol-rekt analysis and chunk pipeline, then validates and exports its `knowledge-base_rag` bundle. The upstream project on which that fork is based is https://github.com/avishek-sen-gupta/cobol-rekt.

## Setup and daily use

New machine, or unsure what to run? **[SETUP.md](SETUP.md)** covers prerequisites,
cloning, the run order, tests, and where every answer's evidence lives.

Note that the model runs on **host Ollama**, not the `ollama` container: compose
points at `host.docker.internal:11434` because Docker on macOS cannot reach the
GPU. A new machine needs Ollama installed and two models pulled.

## One-command Docker run

Copy the environment template if paths differ:

```bash
cp .env.example .env
```

Validate mounts and inputs:

```bash
docker compose run --rm pipeline doctor PDCBVC
```

Run all three repositories, automatically building cobol-rekt with JDK 21, pulling missing Ollama models, and reusing unchanged stages:

```bash
docker compose run --rm pipeline run PDCBVC
```

Start the indexed API and UI:

```bash
docker compose up rag-api
```

Open `http://localhost:8000`.

The compose file binds the API to `127.0.0.1` because this development service
has no authentication. Do not expose port 8000 directly to the internet; use an
authenticated reverse proxy for any shared deployment.

The first Docker build and first Ollama model pull are intentionally slower. Later runs hash their inputs and skip unchanged work.

## Host run

Python 3.11 or newer is required. Install only this dependency-free orchestrator:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cobol-platform doctor PDCBVC
cobol-platform run PDCBVC
```

For host indexing, the configured RAG Python must have `cobol-rag-pipeline` dependencies installed and Ollama must be running. Repository paths and Python executables can be overridden with `ANALYSIS_REPO`, `COBOL_REKT_REPO`, `RAG_REPO`, `ANALYSIS_PYTHON`, and `RAG_PYTHON`.

## Program input contract

Each program has `programs/PROGRAM/program.toml`:

```toml
[program]
name = "PDCBVC"
cobol_source = "input/PDCBVC/PDCBVC.CBL"
copybooks = "input/PDCBVC/copybooks"
mapa = "input/PDCBVC/PDCBVC_result.txt"
controlflow = "input/PDCBVC/PDCBVC_controlflow.json"
# jcl = "input/PDCBVC/jcl"
# rekt_bundle = "input/PDCBVC/knowledge-base_rag"
```

Paths are relative to the configured analysis repository. Add another program by copying this small manifest; no code changes are required.

## Overriding the cobol-rekt exporter

The included exporter runs `analyze.py` in RAG-only mode and then runs `chunk_pipeline.py`. If its interface changes, override it as an argument array so spaces in paths work on every OS:

```toml
[rekt]
python = "python3"
required = true
command = [
  "{rekt_python}",
  "scripts/export_rag_bundle.py",
  "--program", "{program}",
  "--source", "{source}",
  "--copybooks", "{copybooks}",
  "--output", "{rekt_output}",
]
output = "{run_dir}/rekt/knowledge-base_rag"
```

Available placeholders are `{program}`, `{source}`, `{source_name}`, `{copybooks}`, `{mapa}`, `{controlflow}`, `{run_dir}`, `{prepared_program_dir}`, `{rekt_output}`, `{rekt_repo}`, `{rekt_python}`, and `{analysis_output}`. A successful exporter must create `manifest.json` either directly in the output directory or in an immediate `knowledge-base_rag/` child.

## Useful commands

```bash
cobol-platform plan PDCBVC
cobol-platform run PDCBVC --dry-run
cobol-platform run PDCBVC --stop-after analysis
cobol-platform run PDCBVC --force
cobol-platform status PDCBVC
cobol-platform serve PDCBVC --port 8000
```

Analysis state is isolated in `.runs/PROGRAM/`; source repositories are mounted read-only in Docker. Each completed program is incrementally published into `.runs/_corpus/rag/`, so the API searches one collection and enforces the program resolved from the question. Stage fingerprints include input contents, relevant pipeline code, configuration, model names, and the selected evidence bundle.

Adding another program does not reset programs already indexed:

```bash
docker compose run --rm pipeline run PROGA
docker compose run --rm pipeline run PROGB
docker compose up rag-api
```

The shared `corpus.registry.json` is a small deterministic routing catalogue. It resolves program and COBOL entity names before vector retrieval; if a corpus contains multiple programs and a question names none, the assistant asks the user to select one rather than leaking evidence from an arbitrary program.

## RAG Reliability Outputs

The one-command run now creates these shared runtime directories:

```text
.runs/_corpus/rag/data/traces/     per-answer route/retrieval/guard traces
.runs/_corpus/rag/data/feedback/   user feedback linked to trace IDs
.runs/_corpus/rag/data/eval/       JSON and Markdown gold-evaluation reports
.runs/_corpus/rag/final_scripts/   program-separated direct evidence
```

The RAG runtime compiles each request into a typed query plan (program, multiple entities, intent, operations, positive/negative filters, qualifiers, and requested fields). Deterministic evidence handlers and program-filtered hybrid retrieval execute the same plan, with explicit-only follow-up state, corrective exact-identifier lookup, bounded parent/sibling expansion, plan-contract and claim-level validation, and evidence-based abstention.

Run the checked-in PDCBVC gold suite against the live Docker models and collection:

```bash
docker compose exec \
  -e PYTHONPATH=/repos/rag/src \
  rag-api python -m cobol_rag.evaluation \
    --config /workspace/.runs/_corpus/rag/config/runtime.yaml \
    --gold /repos/rag/evals/pdcbvc_gold.jsonl \
    --final-scripts-dir /workspace/.runs/_corpus/rag/final_scripts \
    --output-dir /workspace/.runs/_corpus/rag/data/eval
```

The API exposes answer traces at `/api/traces` and corrective feedback at `/api/feedback`.

## License

This project is released under the [MIT License](LICENSE).
