from fastapi import FastAPI
from pydantic import BaseModel

from db.store import get_recent_runs, get_success_rate

app = FastAPI(title="Pipeline Status")


class RunRecord(BaseModel):
    run_id: str
    started_at: str
    finished_at: str | None
    status: str
    steps_completed: int


class StatusResponse(BaseModel):
    runs: list[RunRecord]
    total_runs: int
    success_rate: float | None


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    runs = get_recent_runs(limit=10)
    return StatusResponse(
        runs=[RunRecord(**r) for r in runs],
        total_runs=len(runs),
        success_rate=get_success_rate(),
    )
