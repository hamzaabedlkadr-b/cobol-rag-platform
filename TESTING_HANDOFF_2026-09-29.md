# COBOL RAG testing handoff — 29 September 2026

This is the latest testing checkpoint, not a claim that every answer is correct.
Use `wip/2026-09-15` in BOTH cobol-rag-platform and cobol-rag-pipeline.
This document supersedes the older feature-branch/Granite startup instructions.

## 1. Requirements and model choice

Install Git, Docker Desktop with Compose v2, and a current Ollama on the host.
Start Docker Desktop and Ollama before proceeding. Budget at least 25 GB free
disk for models, images and artifacts; more may be needed for additional programs.
16 GB RAM is a starting point, not a guarantee that every model/context fits.
The container provides Java and Python; no host Java/Python is required.

The tested Apple Silicon machine uses:

```sh
ollama pull gemma4:e4b-mlx
ollama pull mxbai-embed-large:latest
ollama list
```

The MLX tag is an Apple Silicon choice. On Windows or a non-MLX host, use:

```sh
ollama pull gemma4:e4b
ollama pull mxbai-embed-large:latest
```

Set the exact installed tag in `.env`. The non-MLX model is a different runtime/
quantization configuration; do not assume identical answers or performance.
Public tags: https://ollama.com/library/gemma4/tags

Native host Ollama is used at `http://host.docker.internal:11434` from Docker
Desktop. macOS inference should stay outside Docker to use the Apple GPU.
For a remote Ollama server, use its reachable address instead; pull/install the
models on that server, not just on the client PC. Linux may need separate host
gateway configuration. Do not expose Ollama publicly without access controls.

## 2. New installation: sibling repositories

Ensure you have access to the repositories and required analyzer submodules.
Run the following commands individually in Terminal or PowerShell:

```sh
mkdir Camera
cd Camera
git clone --branch feature/program-capability-manifest https://github.com/hamzaabedlkadr-b/legacy-program-analysis.git control_flow
git clone --branch feature/integration-research https://github.com/erminlilaj/cobol-rekt.git cobol-rekt
git clone --branch wip/2026-09-15 https://github.com/erminlilaj/cobol-rag-pipeline.git cobol-rag-pipeline
git clone --branch wip/2026-09-15 https://github.com/hamzaabedlkadr-b/cobol-rag-platform.git cobol-rag-platform
git -C cobol-rekt submodule update --init --recursive che-che4z-lsp-for-cobol-integration mojo-common woof
```

The analyzer's `.gitmodules` includes SSH URLs. If SSH authentication is not
configured, use this per-command HTTPS rewrite (GitHub credentials may still
be required for private repositories):

```sh
git -C cobol-rekt -c url.https://github.com/.insteadOf=git@github.com: submodule update --init --recursive che-che4z-lsp-for-cobol-integration mojo-common woof
```

## 3. Existing installation: update without overwriting local work

From the parent Camera directory, check `git status --short` in each checkout.
Commit or safely preserve local changes before switching branches. Do not reset
or overwrite `.env`, inputs, manifests or `.runs`.

```sh
git -C cobol-rag-pipeline fetch origin
git -C cobol-rag-pipeline switch wip/2026-09-15
git -C cobol-rag-pipeline pull --ff-only origin wip/2026-09-15
git -C cobol-rag-platform fetch origin
git -C cobol-rag-platform switch wip/2026-09-15
git -C cobol-rag-platform pull --ff-only origin wip/2026-09-15
```

If Git reports divergent history or conflicting local changes, stop and resolve
that explicitly; do not force-reset. No analyzer source changes are included in
this checkpoint. Existing compatible analyzer checkouts can remain unchanged.

## 4. Local environment settings

From `cobol-rag-platform`, copy `.env.example` to `.env` ONLY if `.env` does not
already exist (`cp .env.example .env` on macOS, `Copy-Item .env.example .env` in
PowerShell). Edit `.env`, not `.env.example`. Preserve your local path overrides.

```dotenv
ANALYSIS_REPO=../control_flow
RAG_REPO=../cobol-rag-pipeline
COBOL_REKT_REPO=../cobol-rekt
COBOL_RAG_LLM_MODEL=gemma4:e4b-mlx
COBOL_RAG_LLM_THINKING=false
COBOL_RAG_EMBEDDING_MODEL=mxbai-embed-large:latest
COBOL_RAG_LLM_BASE_URL=http://host.docker.internal:11434
COBOL_RAG_EMBEDDING_BASE_URL=http://host.docker.internal:11434
COBOL_RAG_INVESTIGATION=1
COBOL_RAG_MEMORY_ENABLED=false
```

On Windows/non-MLX hosts change ONLY the model line above to
`COBOL_RAG_LLM_MODEL=gemma4:e4b`. Environment variables already set in your shell
can override `.env`; remove stale model overrides or set them consistently.
Never commit `.env` or credentials. Memory/follow-ups are intentionally disabled;
write self-contained questions naming the program and entity.

## 5. Build and create the local corpus

From `cobol-rag-platform`:

```sh
docker compose build pipeline rag-api
docker compose run --rm pipeline doctor PDCBVC
docker compose run --rm pipeline run PDCBVC
docker compose run --rm pipeline doctor PDB305
docker compose run --rm pipeline run PDB305
```

Proceed only when each command succeeds. The two manifests must point to real
inputs in `control_flow`. PDB305's existing manifest uses `input/PD305/` (not
`input/PDB305/`). Git does not transfer your local `.runs` corpus or model files.
An existing corpus does not need reindexing for these RAG-only repairs. Re-run
the pipeline when source, copybooks, MAPA, CFG or analysis logic changes.

## 6. Start/recreate the API

With host Ollama already running and the image built:

```sh
docker compose up -d --no-deps --force-recreate rag-api
```

Changing `.env` requires recreation, not just restart. For later mounted RAG
code-only updates, `docker compose restart rag-api` normally suffices.
Platform/image/dependency changes require a rebuild and recreation.

Check health on macOS:

```sh
curl http://localhost:8000/api/health
```

Or PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/api/health
```

Check `status=ok`, the selected Gemma `llm` tag,
`embedding=mxbai-embed-large:latest`, and `investigation_enabled=true`.
Health confirms configuration/liveness, not answer correctness or a complete
corpus. Then open http://localhost:8000 and refresh the page.

## 7. Adding another program

Put `NEWPROG.CBL`, `copybooks/`, `NEWPROG_result.txt` and
`NEWPROG_controlflow.json` in `control_flow/input/NEWPROG/`. Optional JCL can go
in `jcl/`. Create `cobol-rag-platform/programs/NEWPROG/program.toml`:

```toml
[program]
name = "NEWPROG"
cobol_source = "input/NEWPROG/NEWPROG.CBL"
copybooks = "input/NEWPROG/copybooks"
mapa = "input/NEWPROG/NEWPROG_result.txt"
controlflow = "input/NEWPROG/NEWPROG_controlflow.json"
# jcl = "input/NEWPROG/jcl"
```

Paths are relative to the analysis repository. Then run:

```sh
docker compose run --rm pipeline doctor NEWPROG
docker compose run --rm pipeline run NEWPROG
docker compose restart rag-api
```

The pipeline analyzes and indexes the program in the shared corpus. Do not
reset the collection when adding a program. Name the intended program in
questions; use explicit program names for comparisons.

## 8. Testing and reporting

This checkpoint includes paragraph-evidence/protocol repairs and HTML chat
downloads, with or without debug details. Memory remains disabled. Verification:
140 focused Python tests and 3 chat-export JavaScript tests passed. Live checks
still exposed omitted qualifiers and a LENGTH explanation error; an accepted
validator result does not prove every technical claim is correct.

Try independent questions about inventory, variables, calls, copybooks and
paragraph behavior. Repeat a question to check consistency. Use **Download +
Debug** to send an HTML transcript containing timestamps, trace identifiers and
runtime metadata. Review exports for private code before sharing. Record:

```sh
git -C ../cobol-rag-pipeline rev-parse HEAD
git rev-parse HEAD
ollama --version
ollama list
```

Do not expose the unauthenticated API on port 8000 to the internet. Keep the
default localhost binding. This is a testing handoff, not a production release.
