import logging
from pathlib import Path
import pandas as pd

log = logging.getLogger(__name__)

CSV_PATH = Path("data/sample.csv")


def build_steps(
    csv_path: Path = CSV_PATH,
    initial_state: dict | None = None,
) -> tuple[list[tuple[str, callable]], dict]:
    """
    Returns (steps, state) instead of just steps.

    Why: state used to be a closure-local dict, invisible outside
    build_steps(). Checkpointing only ever recorded WHICH step names
    succeeded (in SQLite) -- never what they actually produced. On
    resume, skipping a "completed" step meant its output (state['df'])
    was never repopulated in the new process, so the next step reached
    for data that was never there -- KeyError, not a resume.

    Exposing state lets the caller persist it after each step and
    reinject it on resume via initial_state, so a skipped step's
    prior output is actually restored, not just assumed.
    """
    state: dict = initial_state if initial_state is not None else {}

    def load_csv() -> None:
        df = pd.read_csv(csv_path)
        state["df"] = df
        log.info("loaded %d rows, columns: %s", len(df), list(df.columns))

    def cast_types() -> None:
        df = state["df"]
        state["df"] = df.assign(amount=df["amount"].astype(float))
        log.info("cast 'amount' to float")

    def aggregate() -> pd.DataFrame:
        df = state["df"]
        summary = (
            df.groupby("category")["revenue"]
            .agg(total="sum", count="count")
            .reset_index()
        )
        state["summary"] = summary
        log.info("aggregation result:\n%s", summary.to_string(index=False))
        return summary

    steps = [
        ("load_csv",   load_csv),
        ("cast_types", cast_types),
        ("aggregate",  aggregate),
    ]
    return steps, state