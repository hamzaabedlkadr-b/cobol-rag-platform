# Setup and daily use

Four repositories run as one system. This file covers what to install on a new
machine, what to run on a machine that is already set up, and where to look when
you want to read the code or check a fact yourself.

---

## 1. What you need on a new machine

| Requirement | Why | Check it |
|---|---|---|
| **Docker Desktop** | runs the pipeline and the API | `docker info` |
| **Ollama, on the host** | the model runs outside Docker (see the note below) | `ollama --version` |
| **git** | cloning | `git --version` |
| ~15 GB free disk | image + two models + generated artifacts | |
| 16 GB RAM recommended | the 8B model is the constraint | |

**No Java or Python needed on the host.** JDK 21 is inside the Docker image, and
the Python that runs the pipeline is in the image too. You only need host Python
if you want to run the orchestrator outside Docker.

### The one thing that is easy to miss

Compose points the model at **`host.docker.internal:11434`**, which is Ollama
running on your machine, **not** the `ollama` container. On macOS, Docker cannot
reach the Apple GPU, so a model inside the container runs on CPU and is very
slow. Ollama on the host uses Metal.

So on a new machine you must install Ollama yourself and pull two models:

```bash
ollama pull granite-code:8b-instruct
ollama pull mxbai-embed-large
```

`granite-code:8b-instruct` is ~4.6 GB, `mxbai-embed-large` ~669 MB.

To use the container's Ollama instead (Linux, or a machine with no host Ollama),
set `COBOL_RAG_LLM_BASE_URL=http://ollama:11434` and the same for
`COBOL_RAG_EMBEDDING_BASE_URL` in `.env`.

---

## 2. Clone the repositories

They must be **siblings in one folder**. Compose mounts them by relative path,
so a different layout will fail at `doctor`.

```text
Camera/
  control_flow/          analysis: COBOL -> artifacts
  cobol-rag-pipeline/    RAG: retrieval, answering, API and UI
  cobol-rag-platform/    orchestration: this repo, the one you run
  cobol-rekt/            second analyzer and RAG evidence exporter
```

```bash
mkdir Camera && cd Camera
git clone --branch feature/program-capability-manifest \
  https://github.com/hamzaabedlkadr-b/legacy-program-analysis.git control_flow
git clone --branch feature/integration-research \
  https://github.com/erminlilaj/cobol-rekt.git cobol-rekt
git clone --branch feature/semantic-capability-routing \
  https://github.com/erminlilaj/cobol-rag-pipeline.git cobol-rag-pipeline
git clone --branch feature/map-entity-registry \
  https://github.com/hamzaabedlkadr-b/cobol-rag-platform.git cobol-rag-platform
```

If your folder layout differs, copy `.env.example` to `.env` and set
`ANALYSIS_REPO`, `RAG_REPO`, and `COBOL_REKT_REPO`.

---

## 3. Run it

From `cobol-rag-platform/`, in this order.

**Step 1 — start Docker Desktop.** Wait until `docker info` succeeds.

**Step 2 — start Ollama.** It usually runs on login. Confirm:

```bash
curl -s http://localhost:11434/api/tags | head -c 80
```

Anything other than JSON means Ollama is not running; start it with `ollama serve`
or by opening the Ollama app.

**Step 3 — check the setup before running anything:**

```bash
docker compose run --rm pipeline doctor PDCBVC
```

This validates mounts, inputs and models. Fix what it reports before continuing.

**Step 4 — build the artifacts and the index for a program:**

```bash
docker compose run --rm pipeline run PDCBVC
```

First run is slow: it builds the image and reads every stage. Later runs hash
their inputs and skip unchanged work. Run it once per program (`PDB305` too).

**Step 5 — start the API and UI:**

```bash
docker compose up -d rag-api
```

Open **http://localhost:8000** and ask questions there.

The API is intentionally bound to `127.0.0.1`. It has no authentication and is
designed for local development, so do not expose port 8000 directly to the
internet. Put an authenticated reverse proxy in front of it before any shared
or remote deployment.

### After you change code

| You changed | What to do |
|---|---|
| `cobol-rag-pipeline` (the RAG) | `docker compose restart rag-api` — it is mounted live |
| `control_flow` (the analyzer) | re-run `pipeline run <PROGRAM>`, then restart `rag-api` |
| `cobol-rag-platform` | rebuild: `docker compose build` |

The analyzer repo is mounted read-only into the container, so an edit is visible
immediately, but the **artifacts it produced are not** — they only change when
you re-run the pipeline.

---

## 4. Running the tests

**Use `pytest`, not `unittest`.** The repositories mix two test styles and
`unittest discover` silently collects only one of them — it reported 26 tests
where pytest ran 40, and said nothing about the difference.

Analysis repo:

```bash
docker run --rm --entrypoint python -v "$PWD/../control_flow":/cf -w /cf \
  cobol-rag-platform-rag-api -m pytest tests -q
```

RAG repo:

```bash
docker run --rm --entrypoint python -e PYTHONPATH=/rag/src \
  -v "$PWD/../cobol-rag-pipeline":/rag -w /rag \
  cobol-rag-platform-rag-api -m pytest tests -q
```

Run `pytest tests`, not bare `pytest` — from the analysis root, a bare run
descends into an archived vendored repository.

---

## 5. Where the answers come from

Everything the system says about a program is read from files under:

```text
cobol-rag-platform/.runs/_corpus/rag/final_scripts/<PROGRAM>/
```

You can open any of these and check a claim yourself.

| File | What it holds |
|---|---|
| `program.source_lines.jsonl` | **the program, line for line** — the source of truth |
| `controlflow.cfg.json` | paragraphs (`nodes`) and jumps between them (`edges`, with `condition` and `line`) |
| `dataflow.used_variables.json` | every variable, where it is written and read, and its declaring copybook |
| `dataflow.literal_assignments.json` | every hard-coded value moved into a field |
| `architecture.call_parameters.json` | outgoing calls and the fields passed to them |
| `architecture.cics_operations.json` | every `EXEC CICS` statement |
| `architecture.copybooks.json` | COPY members and the section each is included in |
| `screen_field_lineage.json` | BMS screen fields and what feeds them |
| `quality.reconciliation_report.json` | **whether the artifacts agree with the source** — read this first if a number looks wrong |

### Looking at one paragraph

A COBOL paragraph is a named block of statements — the unit the program jumps
between. To see one:

```bash
grep -n "PARAGRAPH-NAME" .runs/_corpus/rag/final_scripts/PDCBVC/controlflow.cfg.json
```

That gives its edges: what reaches it, what it reaches, and under what
condition. For the code itself, ask the UI *"show me lines N to M of PDCBVC"*,
or read `program.source_lines.jsonl`, where each line carries its division,
section and paragraph.

---

## 6. Reading the code

Honest state: **functions have docstrings, and comments explain the non-obvious
decisions, but two files are large.**

| File | Lines | What it does |
|---|---:|---|
| `cobol_rag/query.py` | ~5,200 | the request path: route, plan, execute, validate, answer |
| `cobol_rag/final_scripts_answers.py` | ~4,200 | the capabilities — one function per kind of question, each reading artifacts directly |
| `cobol_rag/query_plan.py` | ~2,200 | turns a question into a plan, and checks an answer against it |
| `cobol_rag/scope.py` | ~900 | which program and which entities a question is about |
| `cobol_rag/query_ir.py` | ~400 | typed queries: the shape of a question, separate from its wording |
| `cobol_rag/retrieve.py` | ~1,400 | vector retrieval, used when no capability applies |

**Start with `query_ir.py`.** It is the smallest, the newest, and its header
explains the central idea: a question is compiled into a typed shape rather than
matched against phrases.

Then read `final_scripts_answers.py` by searching for `def answer_`. Each of
those is one question type, and each reads named artifacts, so you can follow a
claim from the answer back to the file it came from.

Comments in this codebase explain **why**, not what — usually the failure that
motivated the code. If a comment describes a bug, that bug was real and the code
around it is the fix.

### Following a single answer end to end

1. Ask the question in the UI at `http://localhost:8000`
2. The reply names its source, e.g. ``Source: `controlflow.cfg.json` ``
3. Open that file under `.runs/_corpus/rag/final_scripts/<PROGRAM>/`
4. The function that produced it is in `final_scripts_answers.py`; search for
   the heading text from the answer

---

## 7. When something looks wrong

- **A number seems off** — check `quality.reconciliation_report.json`. It runs
  seven consistency checks on every pipeline run and lists anything that
  disagrees with the source.
- **An answer is stale** — you changed the analyzer but did not re-run
  `pipeline run <PROGRAM>`, or did not restart `rag-api`.
- **Everything is slow** — one model serves one request at a time. A long
  request blocks the next one.
- **The first request after idle is slow** — the model is loading; the next is
  normal.
