from types import SimpleNamespace
from unittest.mock import patch

from agent.llm import FixSuggestion
from pipelines.sample_pipeline import build_steps
import main


def test_validated_patch_is_executed_before_the_step_is_marked_successful():
    steps, state = build_steps()
    by_name = dict(steps)
    by_name["load_csv"]()
    failed_fn = by_name["cast_types"]
    patch_code = '''def cast_types() -> None:
    df = state["df"]
    state["df"] = df.assign(revenue=pd.to_numeric(df["revenue"], errors="coerce")).dropna(subset=["category", "revenue"])
'''

    def step_runner(name, fn):
        try:
            return fn()
        except Exception as exc:
            raise RuntimeError(f"Step '{name}' failed (failure_id=1)") from exc

    with (
        patch("main.run_step", side_effect=step_runner),
        patch("main.suggest_fix", return_value=FixSuggestion(diagnosis="repair", code_patch=patch_code)),
        patch("main.validate_patch", return_value=SimpleNamespace(ok=True, reason="valid", detail={})),
        patch("main._persist_patch") as persist,
    ):
        assert main.handle_step("cast_types", failed_fn, state) is True

    persist.assert_called_once()
    assert state["df"]["revenue"].dtype.kind in "fi"
    assert len(state["df"]) == 1
