import inspect
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agent.patcher import merge_patch

SANDBOX_IMAGE = "etl-agent-sandbox:latest"


@dataclass
class ValidationResult:
    ok: bool
    reason: str
    detail: dict = field(default_factory=dict)


# Runs INSIDE the container. Loads the patched pipeline module, executes
# every step in order against the real CSV, and checks the final step's
# output against a structural contract -- now including dtype, not just
# presence/nulls. {expected_columns!r} and {numeric_columns!r} are the only
# substitution points -- everything else is literal, so brace-doubling
# below is deliberate (this gets passed through str.format()).
_HARNESS_TEMPLATE = '''
import importlib.util
import json
import sys
import traceback
from pathlib import Path

import pandas as pd

spec = importlib.util.spec_from_file_location("patched_pipeline", "/sandbox/pipeline.py")
mod = importlib.util.module_from_spec(spec)

try:
    spec.loader.exec_module(mod)
    steps, _state = mod.build_steps(csv_path=Path("/sandbox/sample.csv"))

    last_result = None
    for name, fn in steps:
        last_result = fn()

    summary = last_result
    if summary is None:
        print(json.dumps({{"ok": False, "reason": "pipeline produced no output (last step returned None)"}}))
        sys.exit(1)

    columns = list(summary.columns)
    expected = {expected_columns!r}
    missing = [c for c in expected if c not in columns]
    if missing:
        print(json.dumps({{"ok": False, "reason": "missing expected columns", "detail": {{"missing": missing, "got": columns}}}}))
        sys.exit(1)

    if len(summary) == 0:
        print(json.dumps({{"ok": False, "reason": "output is empty"}}))
        sys.exit(1)

    nulls = {{k: int(v) for k, v in summary.isnull().sum().to_dict().items()}}
    if any(v > 0 for v in nulls.values()):
        print(json.dumps({{"ok": False, "reason": "nulls in output", "detail": {{"nulls": nulls}}}}))
        sys.exit(1)

    # dtype check: a null-check alone does NOT catch a string like
    # "not_a_number" leaking into a numeric column -- it's non-null, just
    # wrong. If unparseable source data got silently passed through
    # instead of properly converted, the column's dtype degrades from
    # float64/int64 to object, and THAT is what's actually checkable here.
    numeric_cols = {numeric_columns!r}
    bad_dtypes = {{}}
    for col in numeric_cols:
        if col in summary.columns and not pd.api.types.is_numeric_dtype(summary[col]):
            bad_dtypes[col] = str(summary[col].dtype)
    if bad_dtypes:
        print(json.dumps({{
            "ok": False,
            "reason": "non-numeric dtype in expected-numeric column (likely unconverted/leaked bad data)",
            "detail": {{"bad_dtypes": bad_dtypes, "sample_values": {{c: summary[c].astype(str).tolist()[:5] for c in bad_dtypes}}}}
        }}))
        sys.exit(1)

    print(json.dumps({{"ok": True, "reason": "pipeline ran end-to-end with valid output", "detail": {{"rows": len(summary), "columns": columns}}}}))
except Exception as exc:
    print(json.dumps({{"ok": False, "reason": f"{{type(exc).__name__}}: {{exc}}", "detail": {{"traceback": traceback.format_exc()[-1500:]}}}}))
    sys.exit(1)
'''


def validate_patch(
    fn,
    patch_code: str,
    csv_path: Path,
    expected_columns: list[str],
    numeric_columns: list[str] | None = None,
    timeout: int = 30,
) -> ValidationResult:
    """
    Swap fn's body for patch_code in a throwaway copy of its source file,
    then run the WHOLE pipeline (not just the patched step) end-to-end
    against real sample data, inside a network-isolated container.

    This deliberately validates the whole pipeline rather than the patched
    step in isolation — a patch that fixes cast_types but breaks aggregate
    downstream (e.g. renames a column aggregate() still expects) needs to
    fail here, not pass a narrower per-step check.

    numeric_columns adds a dtype check on top of the null check: a column
    that's non-null but object-dtype (e.g. a string like "not_a_number"
    that a bad cast silently let through instead of properly converting)
    passes a null check while still being wrong. This is what catches that.
    """
    numeric_columns = numeric_columns or []
    source_file = inspect.getsourcefile(fn)
    if source_file is None:
        return ValidationResult(ok=False, reason=f"no source file for {fn.__name__}")

    original_block = inspect.getsource(fn)
    original_contents = Path(source_file).read_text(encoding="utf-8")
    if original_contents.count(original_block) != 1:
        return ValidationResult(
            ok=False,
            reason=f"could not uniquely locate {fn.__name__} in {source_file}",
        )

    patched_block = merge_patch(original_block, patch_code)
    patched_contents = original_contents.replace(original_block, patched_block)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        pipeline_copy = tmp_dir / "pipeline.py"
        pipeline_copy.write_text(patched_contents, encoding="utf-8")

        csv_copy = tmp_dir / "sample.csv"
        shutil.copy2(csv_path, csv_copy)

        harness = tmp_dir / "harness.py"
        harness.write_text(
            _HARNESS_TEMPLATE.format(
                expected_columns=expected_columns,
                numeric_columns=numeric_columns,
            ),
            encoding="utf-8",
        )

        try:
            result = subprocess.run(
                [
                    "docker", "run",
                    "--rm",
                    "--network", "none",
                    "--memory", "256m",
                    "--memory-swap", "256m",
                    "--cpus", "1",
                    "--read-only",
                    "--tmpfs", "/tmp",
                    "--volume", f"{pipeline_copy}:/sandbox/pipeline.py:ro",
                    "--volume", f"{csv_copy}:/sandbox/sample.csv:ro",
                    "--volume", f"{harness}:/sandbox/harness.py:ro",
                    SANDBOX_IMAGE,
                    "python", "/sandbox/harness.py",
                ],
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(ok=False, reason="validation run timed out")

        stdout = result.stdout.strip()
        if not stdout:
            return ValidationResult(
                ok=False,
                reason="no output from validation harness",
                detail={"returncode": result.returncode, "stderr": result.stderr[:500]},
            )

        try:
            payload = json.loads(stdout.splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return ValidationResult(
                ok=False,
                reason="harness output was not valid JSON",
                detail={"stdout": stdout[:500]},
            )

        return ValidationResult(
            ok=payload.get("ok", False),
            reason=payload.get("reason", "unknown"),
            detail=payload.get("detail", {}),
        )