# Self-Correcting Data Pipeline

An autonomous data pipeline that detects its own failures, asks a local LLM to diagnose and patch them, validates the patch in an isolated Docker sandbox, executes the validated repair against the live step state, and escalates to Slack only when it cannot recover on its own.

---

## What It Does

Most pipeline failures are transient or mechanical: a column gets renamed, a type assumption breaks, an upstream schema drifts. This project treats those failures as inputs to an automated repair loop rather than pages to an on-call engineer.

When a pipeline step raises an exception the system:

1. Logs the failure to SQLite with a full traceback
2. Classifies the error into a category (`schema`, `null`, `timeout`, `logic`, or `unknown`)
3. Sends the error and traceback to a local Ollama LLM and asks for a diagnosis and a code patch
4. Syntax-checks the patch without executing it
5. Runs a temporary, patched copy of the full pipeline inside a locked-down Docker container (no network, 256 MB RAM)
6. If validation succeeds, executes that replacement function against the live pipeline state, then records and checkpoints the fix
7. If any stage fails, retries the whole loop up to `MAX_ATTEMPTS` times
8. If all attempts fail, posts a structured alert to a Slack webhook and halts the run

Every step that succeeds is checkpointed to SQLite so a re-run of the script resumes from where it left off rather than starting over.

---

## How the Self-Correcting Loop Works

```
Pipeline step raises an exception
          │
          ▼
   monitor.run_step()
   └─ logs failure to DB → returns failure_id
          │
          ▼
   classifier.classify()
   └─ regex rules → "schema" | "null" | "timeout" | "logic" | "unknown"
          │
          ▼
   llm.suggest_fix()                        (Ollama / llama3.2)
   └─ prompt: error message + traceback
   └─ response: diagnosis + complete replacement function
          │
          ▼
   sandbox.run_patch()                      (syntax check only)
   └─ compile() accepts/rejects Python syntax
          │
          ▼
   validator.validate_patch()                (Docker)
   └─ docker run --network none
                 --memory 256m
                 --read-only
   └─ full pipeline + output contract pass → candidate accepted
   └─ failure or timeout → candidate rejected
          │
     ┌────┴────┐
  accepted   rejected
     │            │
     ▼            ▼
  execute patch against live state
  log/checkpoint fix    attempt < MAX?
  continue      yes → retry loop
              no  → alert() → Slack
```

The entire loop lives in `handle_step()` in `main.py` and is re-entrant — retries call the same function with an incremented attempt counter.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Pipeline orchestration | Python 3.12, custom step runner |
| LLM inference | [Ollama](https://ollama.com) (`llama3.2`) via HTTP |
| Patch validation | Python syntax gate + Docker (`python:3.12-slim`, `--network none`, `--memory 256m`) |
| Error classification | Regex rules (`agent/classifier.py`) |
| Storage | SQLite (`data.db`) — failures, fixes, checkpoints, runs |
| Data processing | pandas |
| Status API | FastAPI + uvicorn |
| Alerting | Slack incoming webhook |
| Logging | Python `logging` → console + `pipeline.log` |
| Config | `python-dotenv` (`.env` file) |

---

## Project Structure

```
.
├── main.py                    # Entry point, self-correcting loop
├── agent/
│   ├── classifier.py          # Regex-based error categorisation
│   ├── escalate.py            # Slack webhook alert
│   ├── llm.py                 # Ollama API client with retry backoff
│   ├── monitor.py             # Step runner, logs failures to DB
│   ├── sandbox.py             # Non-executing patch syntax gate
│   ├── validator.py           # Docker-based full-pipeline validation
│   ├── patcher.py             # Runtime patch construction and source persistence
│   └── state_store.py         # Checkpointed pipeline-state persistence
├── api/
│   └── status.py              # FastAPI /status endpoint
├── db/
│   └── store.py               # SQLite schema + all DB helpers
├── pipelines/
│   └── sample_pipeline.py     # Example three-step CSV pipeline
├── tests/
│   ├── test_classifier.py
│   ├── test_sandbox.py
│   ├── test_patcher.py
│   └── test_repair_loop.py
├── data/
│   └── sample.csv             # Input data for sample pipeline
├── requirements.txt
└── .env                       # Secrets (not committed)
```

---

## Setup

### Prerequisites

- Python 3.12+
- [Docker](https://docs.docker.com/get-docker/) (running, with `python:3.12-slim` pulled or available)
- [Ollama](https://ollama.com) running locally with `llama3.2` pulled

```bash
# Pull the Docker image used by the sandbox
docker pull python:3.12-slim

# Pull the LLM model
ollama pull llama3.2
```

### Install

```bash
git clone <repo-url>
cd <repo>

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pip install fastapi uvicorn   # for the status API
```

### Configure

Create a `.env` file in the project root:

```dotenv
# Required only for Slack escalation — omit to disable alerting
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

---

## How to Run

### Run the pipeline

```bash
python main.py
```

If the run fails mid-way, simply run it again — it will resume from the last successful step automatically.

To force a fresh run, delete `.pipeline_run`:

```bash
rm .pipeline_run   # macOS/Linux
del .pipeline_run  # Windows
```

### Run the status API

```bash
uvicorn api.status:app --reload
```

Open `http://localhost:8000/status` for JSON or `http://localhost:8000/docs` for the interactive Swagger UI.

### Run the tests

```bash
pytest tests/ -v
```

---

## Example Output

A run where `cast_types` fails (the CSV has a `revenue` column, not `amount`), the LLM proposes a fix, Docker validates the patched pipeline, the replacement runs against the live state, and the pipeline resumes:

```
2024-11-14 09:12:03  INFO      __main__  Starting new run 4f3a1b2c-...
2024-11-14 09:12:03  INFO      __main__  Running step: load_csv
2024-11-14 09:12:03  INFO      pipelines.sample_pipeline  loaded 120 rows, columns: ['date', 'category', 'revenue']
2024-11-14 09:12:03  INFO      __main__  Running step: cast_types
2024-11-14 09:12:03  WARNING   __main__  [cast_types] failure_id=1 category=schema
2024-11-14 09:12:03  INFO      __main__  diagnosis: The DataFrame has no 'amount' column; the correct column is 'revenue'. The cast should target 'revenue' instead.
2024-11-14 09:12:04  INFO      __main__  patch validated and executed live: pipeline ran end-to-end with valid output
2024-11-14 09:12:04  INFO      __main__  Running step: aggregate
2024-11-14 09:12:04  INFO      pipelines.sample_pipeline  aggregation result:
  category   total  count
     electronics  4821.50     43
          apparel  2103.75     38
         groceries  1455.20     39
2024-11-14 09:12:04  INFO      __main__  Pipeline completed successfully — run_id 4f3a1b2c-... cleared
2024-11-14 09:12:04  INFO      __main__  --- Pipeline summary ---
2024-11-14 09:12:04  INFO      __main__    load_csv: OK
2024-11-14 09:12:04  INFO      __main__    cast_types: OK
2024-11-14 09:12:04  INFO      __main__    aggregate: OK
```

If the patch is rejected and all retries are exhausted, the Slack alert looks like:

```
🔴 Pipeline failure (attempt 2)
Failure ID: `7`
Step: `cast_types`
Error: KeyError: 'amount'
```

### Status API response

```json
{
  "runs": [
    {
      "run_id": "4f3a1b2c-8d9e-...",
      "started_at": "2024-11-14T09:12:03",
      "finished_at": "2024-11-14T09:12:04",
      "status": "success",
      "steps_completed": 3
    },
    {
      "run_id": "a1b2c3d4-...",
      "started_at": "2024-11-13T22:47:11",
      "finished_at": "2024-11-13T22:47:18",
      "status": "failed",
      "steps_completed": 1
    }
  ],
  "total_runs": 2,
  "success_rate": 0.5
}
```

---

## Future Improvements

**LLM and patching**
- Replace regex classification with an LLM-based classifier that handles novel error shapes
- Strengthen the review/promotion policy for persisted patches; accepted patches run immediately, while source promotion remains opt-in via `AUTO_PROMOTE_PATCHES=1`
- Support multiple patch candidates — generate N suggestions and test each in parallel, taking the first that passes
- Use structured output / tool-use to guarantee the LLM returns valid JSON

**Sandbox**
- Add a resource profile per step so short steps get a tighter timeout and memory limit than heavy ones
- Return stdout/stderr from the container and include it in the diagnosis prompt for a tighter feedback loop
- Pre-build a custom Docker image with common dependencies installed to avoid cold-pull latency

**Pipeline**
- Allow steps to declare explicit dependencies so independent steps can run in parallel
- Support `async` step functions via `asyncio` for I/O-bound pipelines
- Add a dry-run mode that validates the step graph and checks data shapes without executing

**Observability**
- Extend `/status` with per-step timing, failure counts by category, and a `/runs/{run_id}` detail endpoint
- Emit OpenTelemetry spans so the self-correction loop appears as a trace in Grafana or Jaeger
- Persist `pipeline.log` to object storage (S3/GCS) for long-term retention

**Resilience**
- Add a dead-letter queue for failures that exhaust all retries, enabling manual review and replay
- Support configurable back-off between retry attempts at the step level
- Detect and break infinite-resume loops (e.g., a run that has been in `in_progress` for over N hours)
