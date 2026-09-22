# Investigation testing handoff

Use branch `wip/2026-09-15` in BOTH repositories:

- https://github.com/hamzaabedlkadr-b/cobol-rag-platform/tree/wip/2026-09-15
- https://github.com/erminlilaj/cobol-rag-pipeline/tree/wip/2026-09-15

In each existing checkout, preserve local changes, then run:

```sh
git fetch origin
git switch wip/2026-09-15
git pull --ff-only
```

For a first installation, follow SETUP.md for the analysis dependencies, inputs,
corpus generation and Docker build. Pulling code does not copy the original
machine's analyzed corpus, local model files, runtime configuration or secrets.

## Match the tested configuration

Edit the platform's local `.env` (copy `.env.example` only if `.env` does not
already exist). Preserve your repository paths and endpoint settings. Set:

```dotenv
COBOL_RAG_LLM_MODEL=gemma4:e4b-mlx
COBOL_RAG_EMBEDDING_MODEL=mxbai-embed-large:latest
COBOL_RAG_INVESTIGATION=1
```

`gemma4:e4b-mlx` is the exact tag reported by the tested local API. Its presence
on another machine is NOT guaranteed; check that the configured model server
actually provides that tag. Do not assume it is a downloadable public alias or
that another Gemma tag is identical. Set `COBOL_RAG_LLM_BASE_URL` and
`COBOL_RAG_EMBEDDING_BASE_URL` to endpoints reachable from the API container.
Docker Desktop defaults point to native host Ollama; Linux/remote hosts may
require different addresses. Never commit your local `.env`.

For an existing built installation with the model server already running:

```sh
docker compose up -d --no-deps --force-recreate rag-api
curl http://localhost:8000/api/health
```

Verify `llm` is `gemma4:e4b-mlx`, `embedding` is
`mxbai-embed-large:latest`, and `investigation_enabled` is true. Rebuild the image
if Docker dependencies changed; see SETUP.md. Refresh the UI and start a fresh
chat. Existing stored messages will not acquire the new debug fields.

## State of the work

This is a testing checkpoint, not a fully validated release. It includes
follow-up context and collection repairs, general-question handling, evidence
and execution-trace visibility, and depth-aware explanation retrieval.
93 focused offline tests passed. Brief and richer single-program explanations
were verified live. Detailed multi-program comparisons remain unreliable:
observed failures include missing retrieval, shallow coverage, and incorrect
call-kind/count wording. New checks reject some of those contradictions but do
not establish universal correctness. See the pipeline's INVESTIGATION.md.

Test greetings, capability/file discovery, a one-sentence explanation, a detailed
explanation, and comparisons separately. Inspect returned facts, not just the
validator's accepted label. Keep question, answer, trace ID, model and commit
IDs when reporting a failure. Local logs, generated package metadata, model
files and runtime corpus data are not included in this checkpoint.
