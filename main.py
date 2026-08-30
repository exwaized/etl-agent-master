import inspect
import re
import traceback
import uuid
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from db.store import init_db, log_fix, mark_fix_applied, save_checkpoint, load_checkpoints, start_run, finish_run
from agent.state_store import save_state, load_state, clear_state
from agent.monitor import run_step
from agent.classifier import classify
from agent.llm import suggest_fix
from agent.sandbox import run_patch
from agent.validator import validate_patch
from agent.patcher import apply_patch_to_source, build_runtime_function, PatchApplyError
from agent.escalate import alert
from pipelines.sample_pipeline import build_steps, CSV_PATH

MAX_ATTEMPTS = 4
_RUN_ID_FILE = Path(".pipeline_run")

_EXPECTED_OUTPUT_COLUMNS = ["category", "total", "count"]
_NUMERIC_OUTPUT_COLUMNS = ["total", "count"]

log = logging.getLogger(__name__)


def _get_or_create_run_id() -> tuple[str, bool]:
    if _RUN_ID_FILE.exists():
        run_id = _RUN_ID_FILE.read_text().strip()
        return run_id, True
    run_id = str(uuid.uuid4())
    _RUN_ID_FILE.write_text(run_id)
    return run_id, False


def _clear_run_id() -> None:
    _RUN_ID_FILE.unlink(missing_ok=True)


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = logging.FileHandler("pipeline.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)


def _extract_failure_id(msg: str) -> int | None:
    m = re.search(r"failure_id=(\d+)", msg)
    return int(m.group(1)) if m else None


def _persist_patch(fn, patch_code: str, failure_id: int | None, diagnosis: str) -> None:
    if failure_id is None:
        return
    fix_id = log_fix(failure_id, diagnosis)
    try:
        source_file, backup_path, promoted = apply_patch_to_source(fn, patch_code)
        mark_fix_applied(fix_id, source_file, backup_path, promoted)
        if promoted:
            log.info("patch promoted to %s (backup: %s)", source_file, backup_path)
        else:
            log.info(
                "patch written to %s.patched for review (backup: %s) — "
                "set AUTO_PROMOTE_PATCHES=1 to auto-promote once validation is stronger",
                source_file, backup_path,
            )
    except PatchApplyError as exc:
        log.warning("sandbox accepted patch but could not persist to source: %s", exc)


def handle_step(name: str, fn, state: dict, attempt: int = 1, previous_attempt_feedback: str | None = None) -> bool:
    try:
        run_step(name, fn)
        return True
    except RuntimeError as exc:
        failure_id = _extract_failure_id(str(exc))
        error_msg = str(exc.__cause__) if exc.__cause__ else str(exc)
        category = classify(error_msg)
        log.warning("[%s] failure_id=%s category=%s", name, failure_id, category)

        try:
            original_source = inspect.getsource(fn)
        except OSError:
            original_source = None

        data_context = None
        df = state.get("df")
        if df is not None and hasattr(df, "columns"):
            data_context = f"Current dataframe columns: {list(df.columns)}"

        # Full tracebacks from pandas internals can be 2000+ chars — way
        # too much for llama3.2's 2048 default context when combined with
        # the system prompt, original source, data context, and feedback.
        # The last few frames (the actual user code that failed) carry all
        # the diagnostic value; the deep pandas/numpy frames are noise.
        raw_context = "".join(traceback.format_exception(exc.__cause__)) if exc.__cause__ else None
        truncated_context = raw_context[-500:] if raw_context else None

        try:
            suggestion = suggest_fix(
                message=error_msg,
                context=truncated_context,
                failure_id=failure_id,
                original_source=original_source,
                data_context=data_context,
                previous_attempt_feedback=previous_attempt_feedback,
            )
            log.info("diagnosis: %s", suggestion.diagnosis)
        except Exception as llm_exc:
            log.warning("LLM call failed, treating as failed generation: %s", llm_exc)
            suggestion = None

        next_feedback = None
        if suggestion is not None:
            pre_check = run_patch(suggestion.code_patch)
            if not pre_check.accepted:
                log.warning("patch rejected at syntax check (exit=%s): %s",
                            pre_check.returncode, pre_check.stderr.strip()[:300])
                next_feedback = (
                    f"Your previous patch had a syntax error: {pre_check.stderr.strip()[:300]}. "
                    f"Fix that specific problem in this attempt."
                )
            else:
                validation = validate_patch(
                    fn, suggestion.code_patch, CSV_PATH,
                    _EXPECTED_OUTPUT_COLUMNS, _NUMERIC_OUTPUT_COLUMNS,
                )
                if validation.ok:
                    try:
                        runtime_fn = build_runtime_function(fn, suggestion.code_patch)
                        # Execute the validated replacement against the live
                        # closure/state before treating this step as repaired.
                        # Without this, a successful sandbox candidate was
                        # checkpointed while the failed in-memory function had
                        # never changed or run.
                        run_step(name, runtime_fn)
                    except (PatchApplyError, RuntimeError) as runtime_exc:
                        log.warning("validated patch failed in the live pipeline: %s", runtime_exc)
                        next_feedback = (
                            "Your patch passed isolated validation but failed when applied to the live "
                            f"step: {runtime_exc}. Propose a different fix that avoids this problem."
                        )
                    else:
                        _persist_patch(fn, suggestion.code_patch, failure_id, suggestion.diagnosis)
                        log.info("patch validated and executed live: %s", validation.reason)
                        return True
                else:
                    log.warning("patch rejected at validation: %s | %s",
                                validation.reason, str(validation.detail)[:300])
                    # This is the actual fix for the "retries have no memory" gap:
                    # without this, every retry re-diagnoses the ORIGINAL error from
                    # scratch (since nothing was ever persisted) and tends to propose
                    # roughly the same kind of fix, hitting the same secondary wall
                    # every time. Telling the model exactly why its last attempt was
                    # rejected turns retry 2 into an informed correction instead of
                    # a second independent guess.
                    next_feedback = (
                        f"Your previous patch was rejected during validation: {validation.reason}. "
                        f"Detail: {str(validation.detail)[:300]}. "
                        f"Propose a DIFFERENT fix that specifically avoids this problem."
                    )

        if attempt < MAX_ATTEMPTS:
            log.warning("patch failed, retrying (%d/%d)", attempt + 1, MAX_ATTEMPTS)
            return handle_step(name, fn, state, attempt + 1, next_feedback)

        log.warning("escalating to Slack")
        try:
            alert(
                failure_id=failure_id or 0,
                step=name,
                error=error_msg,
                attempts=attempt,
            )
        except ValueError as e:
            log.warning("escalation skipped: %s", e)
        return False


def main():
    _setup_logging()
    init_db()

    run_id, is_resume = _get_or_create_run_id()
    completed = load_checkpoints(run_id)
    start_run(run_id)

    if is_resume and completed:
        log.info("Resuming run %s — skipping %d completed step(s): %s",
                 run_id, len(completed), sorted(completed))
    else:
        log.info("Starting new run %s", run_id)

    # Restore actual data state on resume, not just which step names
    # completed -- previously a skipped step's output (e.g. state['df'])
    # was never repopulated in the new process, so the next step reached
    # for data that was never there.
    restored_state = load_state(run_id) if is_resume else None
    steps, state = build_steps(initial_state=restored_state)

    results = []
    all_ok = True
    for name, fn in steps:
        if name in completed:
            log.info("Skipping step (already done): %s", name)
            results.append((name, True))
            continue

        log.info("Running step: %s", name)
        ok = handle_step(name, fn, state)
        results.append((name, ok))

        if ok:
            save_checkpoint(run_id, name)
            save_state(run_id, state)
        else:
            all_ok = False
            break

    if all_ok:
        finish_run(run_id, "success")
        _clear_run_id()
        clear_state(run_id)
        log.info("Pipeline completed successfully — run_id %s cleared", run_id)
    else:
        finish_run(run_id, "failed")

    log.info("--- Pipeline summary ---")
    for name, ok in results:
        status = "OK" if ok else "FAILED"
        log.info("  %s: %s", name, status)


if __name__ == "__main__":
    main()
