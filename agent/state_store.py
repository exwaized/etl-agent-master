import pickle
from pathlib import Path

_STATE_DIR = Path(".pipeline_state")


def save_state(run_id: str, state: dict) -> None:
    """Persist the pipeline's actual data state, not just step names.
    Called after every successful step so a crash mid-run loses at most
    the in-flight step, not everything a resumed run assumed it still had."""
    _STATE_DIR.mkdir(exist_ok=True)
    path = _STATE_DIR / f"{run_id}.pkl"
    with open(path, "wb") as f:
        pickle.dump(state, f)


def load_state(run_id: str) -> dict | None:
    """Returns None if no state was ever saved for this run_id (fresh run)."""
    path = _STATE_DIR / f"{run_id}.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def clear_state(run_id: str) -> None:
    path = _STATE_DIR / f"{run_id}.pkl"
    path.unlink(missing_ok=True)
