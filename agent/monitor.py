import traceback
from typing import Callable, Any

from db.store import log_failure


def run_step(name: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        context = traceback.format_exc()
        failure_id = log_failure(
            message=f"[{name}] {type(exc).__name__}: {exc}",
            context=context,
        )
        raise RuntimeError(f"Step '{name}' failed (failure_id={failure_id})") from exc


def run_pipeline(steps: list[tuple[str, Callable[[], Any]]]) -> list[Any]:
    results = []
    for name, fn in steps:
        results.append(run_step(name, fn))
    return results
