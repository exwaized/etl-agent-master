import inspect
import os
import re
import shutil
import textwrap
from datetime import datetime
from pathlib import Path


def reindent_to_match(original_block: str, patch_code: str) -> str:
    """Re-indent patch_code to match original_block's leading whitespace.

    LLM-generated patches are almost always written at zero indentation.
    But the function being replaced might be top-level OR nested inside
    a closure (as with this pipeline's build_steps() steps) — assuming
    zero indentation and doing a blind dedent corrupts nested functions.
    Detect the real indent from the block we're replacing and apply it,
    rather than assuming."""
    first_line = original_block.splitlines()[0]
    indent = first_line[: len(first_line) - len(first_line.lstrip())]
    normalized = textwrap.dedent(patch_code).strip("\n")
    reindented = "\n".join(
        (indent + line if line.strip() else line)
        for line in normalized.splitlines()
    )
    return reindented + "\n"


_DEF_RE = re.compile(r"^\s*def\s+(\w+)\s*\(")


def merge_patch(original_block: str, patch_code: str) -> str:
    """
    Merge an LLM-generated patch into the original function block.

    LLM patches show up in two shapes, inconsistently, because the
    prompt asks for "a code snippet or unified diff" without pinning
    down which:
      1. A full function redefinition — "def cast_types() -> None: ..."
      2. A bare body fragment — "df = df.dropna() if ... else df"

    Blindly replacing the whole original block with a bare fragment
    deletes the function's own `def` line along with its body, which
    breaks every reference to that function name elsewhere in the file
    (e.g. build_steps()'s return list -> NameError). Detect which shape
    we actually got and only replace the signature when the patch
    provides one of its own.
    """
    lines = original_block.splitlines()
    signature_line = lines[0]
    def_match = _DEF_RE.match(signature_line)

    if def_match is None:
        # Original block isn't a function definition at all (unexpected
        # shape) — fall back to a straight reindent-and-replace rather
        # than guessing further.
        return reindent_to_match(original_block, patch_code)

    original_name = def_match.group(1)
    normalized_patch = textwrap.dedent(patch_code).strip("\n")
    if not normalized_patch:
        return reindent_to_match(original_block, patch_code)

    patch_def_match = _DEF_RE.match(normalized_patch.splitlines()[0])
    if patch_def_match and patch_def_match.group(1) == original_name:
        # Patch already redefines the function with a matching name —
        # treat as a full replacement, same as before.
        return reindent_to_match(original_block, patch_code)

    # Body-only fragment: keep the original signature line untouched,
    # replace only the body beneath it, indented one level deeper.
    indent_match = re.match(r"^(\s*)", signature_line)
    base_indent = indent_match.group(1) if indent_match else ""
    body_indent = base_indent + "    "
    reindented_body = "\n".join(
        (body_indent + line if line.strip() else line)
        for line in normalized_patch.splitlines()
    )
    return f"{signature_line}\n{reindented_body}\n"


def build_runtime_function(fn, patch_code: str):
    """Build a patched version of ``fn`` that shares its live closure state.

    Validation proves a patch works in a disposable container, but the
    pipeline also needs to execute that same patch before it can checkpoint
    the repaired step.  Nested pipeline steps close over objects such as the
    shared ``state`` dict.  A normal ``exec`` would lose that closure, so
    expose each closed-over value in the execution namespace.  The generated
    function then updates the very same objects as the failed function.

    This does not mutate the original function or source file; callers can
    still choose whether to promote the separately persisted patch.
    """
    try:
        original_block = inspect.getsource(fn)
    except OSError as exc:
        raise PatchApplyError(f"no source available for {fn.__name__}: {exc}") from exc

    patched_source = textwrap.dedent(merge_patch(original_block, patch_code))
    namespace = dict(fn.__globals__)
    if fn.__closure__:
        namespace.update({
            name: cell.cell_contents
            for name, cell in zip(fn.__code__.co_freevars, fn.__closure__)
        })

    try:
        exec(compile(patched_source, f"<runtime patch for {fn.__name__}>", "exec"), namespace)
    except Exception as exc:
        raise PatchApplyError(f"could not build runtime patch for {fn.__name__}: {exc}") from exc

    patched_fn = namespace.get(fn.__name__)
    if not callable(patched_fn):
        raise PatchApplyError(f"runtime patch did not define callable {fn.__name__}")
    return patched_fn


class PatchApplyError(Exception):
    """Raised when a patch can't be safely written back to its source file.

    A failed write-back must never corrupt pipeline source — it should
    fall back to session-only healing (today's behaviour) instead.
    """


def apply_patch_to_source(fn, patch_code: str, auto_promote: bool | None = None) -> tuple[str, str, bool]:
    """
    Persist an accepted patch back to fn's source file.

    Returns (source_file, backup_path, promoted).

    auto_promote controls whether the patch overwrites the real source
    file directly, or lands next to it as `<file>.patched` for manual
    review. Defaults to the AUTO_PROMOTE_PATCHES env var (unset = False).

    Why default to False: the sandbox's current acceptance bar is
    "the patch ran without crashing" (see agent/sandbox.py), not "the
    patch produces correct output." That bar is fine for a patch that
    gets discarded after the run. It is not a strong enough bar to let
    a patch permanently rewrite pipeline source unattended. Flip
    AUTO_PROMOTE_PATCHES=1 only once run_patch() validates against real
    output, not just exit code.
    """
    if auto_promote is None:
        auto_promote = os.environ.get("AUTO_PROMOTE_PATCHES", "").lower() in ("1", "true", "yes")

    try:
        source_file = inspect.getsourcefile(fn)
        if source_file is None:
            raise PatchApplyError(f"no source file found for {fn.__name__}")
        original_block = inspect.getsource(fn)
    except OSError as exc:
        # inspect raises a bare OSError (not our exception type) for
        # functions with no real backing source — e.g. eval()'d lambdas,
        # REPL-defined functions. Normalize it so callers only ever
        # need to catch PatchApplyError, not two unrelated exception types.
        raise PatchApplyError(f"no source available for {fn.__name__}: {exc}") from exc

    file_path = Path(source_file)
    original_contents = file_path.read_text(encoding="utf-8")

    # Refuse rather than guess: if the function's current source text
    # doesn't appear exactly once, decorators/duplicate defs/whitespace
    # drift mean we can't be sure we'd be replacing the right block.
    if original_contents.count(original_block) != 1:
        raise PatchApplyError(
            f"could not uniquely locate {fn.__name__} in {source_file} "
            f"({original_contents.count(original_block)} matches, expected 1)"
        )

    backup_path = f"{source_file}.bak.{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(source_file, backup_path)

    patched_block = merge_patch(original_block, patch_code)
    new_contents = original_contents.replace(original_block, patched_block)

    if auto_promote:
        file_path.write_text(new_contents, encoding="utf-8")
        target = source_file
    else:
        target = f"{source_file}.patched"
        Path(target).write_text(new_contents, encoding="utf-8")

    return source_file, backup_path, auto_promote
