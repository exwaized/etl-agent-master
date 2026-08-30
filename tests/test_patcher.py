import importlib.util
import sys
from pathlib import Path

import pytest

from agent.patcher import apply_patch_to_source, build_runtime_function, PatchApplyError


def _load_module(path: Path, name: str):
    """Import a throwaway .py file as a module so its functions have a
    real __code__.co_filename for inspect.getsourcefile() to find —
    mirrors how build_steps() functions are loaded in the real pipeline."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_default_mode_writes_patched_file_and_leaves_source_untouched(tmp_path):
    src = tmp_path / "step_mod_review.py"
    original = (
        "def cast_types(df):\n"
        "    df['amount'] = df['amount'].astype(float)\n"
        "    return df\n"
    )
    src.write_text(original)
    mod = _load_module(src, "step_mod_review")

    patch_code = (
        "def cast_types(df):\n"
        "    df['revenue'] = df['revenue'].astype(float)\n"
        "    return df\n"
    )

    source_file, backup_path, promoted = apply_patch_to_source(mod.cast_types, patch_code)

    assert promoted is False
    assert src.read_text() == original                       # source untouched by default
    assert Path(backup_path).read_text() == original          # backup matches pre-patch state
    patched_file = Path(f"{source_file}.patched")
    assert patched_file.exists()
    assert "revenue" in patched_file.read_text()               # patch landed for review


def test_auto_promote_overwrites_source_and_keeps_backup(tmp_path):
    src = tmp_path / "step_mod_promote.py"
    original = (
        "def cast_types(df):\n"
        "    df['amount'] = df['amount'].astype(float)\n"
        "    return df\n"
    )
    src.write_text(original)
    mod = _load_module(src, "step_mod_promote")

    patch_code = (
        "def cast_types(df):\n"
        "    df['revenue'] = df['revenue'].astype(float)\n"
        "    return df\n"
    )

    source_file, backup_path, promoted = apply_patch_to_source(
        mod.cast_types, patch_code, auto_promote=True
    )

    assert promoted is True
    assert "revenue" in Path(source_file).read_text()          # source now has the fix
    assert "amount" in Path(backup_path).read_text()           # backup preserves the old version


def test_refuses_when_source_block_is_not_unique(tmp_path):
    # Redefining a function with byte-identical source makes the original
    # text appear twice in the file — patcher must refuse rather than
    # guess which occurrence to replace.
    src = tmp_path / "dup_mod.py"
    src.write_text(
        "def cast_types(df):\n"
        "    df['amount'] = df['amount'].astype(float)\n"
        "    return df\n"
        "\n"
        "def cast_types(df):\n"
        "    df['amount'] = df['amount'].astype(float)\n"
        "    return df\n"
    )
    mod = _load_module(src, "dup_mod")

    with pytest.raises(PatchApplyError):
        apply_patch_to_source(mod.cast_types, "def cast_types(df):\n    return df\n")


def test_preserves_indentation_for_nested_closure_functions(tmp_path):
    # Mirrors the real shape of pipelines/sample_pipeline.py: steps are
    # nested inside build_steps(), not top-level. A patcher that assumes
    # zero indentation would corrupt this file's syntax.
    src = tmp_path / "nested_mod.py"
    src.write_text(
        "def build_steps():\n"
        "    state = {}\n"
        "\n"
        "    def cast_types() -> None:\n"
        "        df = state['df']\n"
        "        state['df'] = df.assign(amount=df['amount'].astype(float))\n"
        "\n"
        "    return [('cast_types', cast_types)]\n"
    )
    mod = _load_module(src, "nested_mod")
    steps = mod.build_steps()
    cast_types_fn = dict(steps)["cast_types"]

    patch_code = (
        "def cast_types() -> None:\n"
        "    df = state['df']\n"
        "    state['df'] = df.assign(revenue=df['revenue'].astype(float))\n"
    )

    source_file, _, _ = apply_patch_to_source(cast_types_fn, patch_code)
    patched_text = Path(f"{source_file}.patched").read_text()

    # Must still be valid, correctly-indented Python — this would raise
    # IndentationError before the fix.
    compile(patched_text, source_file, "exec")
    assert "    def cast_types() -> None:" in patched_text  # indent preserved
    assert "revenue" in patched_text


def test_body_fragment_patch_preserves_function_signature(tmp_path):
    # Mirrors the real failure: the LLM's patch was a bare statement,
    # not a full "def cast_types():" redefinition. Blindly replacing the
    # whole original block with that fragment deletes the def line,
    # breaking every reference to cast_types elsewhere in the file.
    src = tmp_path / "frag_mod.py"
    src.write_text(
        "def build_steps():\n"
        "    state = {}\n"
        "\n"
        "    def cast_types() -> None:\n"
        "        df = state['df']\n"
        "        state['df'] = df.assign(amount=df['amount'].astype(float))\n"
        "\n"
        "    return [('cast_types', cast_types)]\n"
    )
    mod = _load_module(src, "frag_mod")
    steps = mod.build_steps()
    cast_types_fn = dict(steps)["cast_types"]

    # Bare fragment -- no "def cast_types" line at all.
    patch_code = "df = state['df']\nstate['df'] = df.assign(revenue=df['revenue'].astype(float))\n"

    source_file, _, _ = apply_patch_to_source(cast_types_fn, patch_code)
    patched_text = Path(f"{source_file}.patched").read_text()

    compile(patched_text, source_file, "exec")  # must still be valid Python
    assert "def cast_types() -> None:" in patched_text  # signature preserved
    assert "revenue" in patched_text  # patched body actually applied


def test_missing_source_file_raises_cleanly():
    # A dynamically-built function (no backing file) must fail loudly,
    # not silently no-op.
    fn = eval("lambda df: df")
    with pytest.raises(PatchApplyError):
        apply_patch_to_source(fn, "lambda df: df")


def test_runtime_patch_updates_the_original_closure_state(tmp_path):
    src = tmp_path / "runtime_mod.py"
    src.write_text(
        "def build_steps():\n"
        "    state = {'value': 1}\n"
        "\n"
        "    def step():\n"
        "        state['value'] = 0\n"
        "\n"
        "    return step, state\n"
    )
    mod = _load_module(src, "runtime_mod")
    step, state = mod.build_steps()

    patched = build_runtime_function(
        step,
        "def step():\n    state['value'] = 42\n",
    )
    patched()

    assert state == {'value': 42}
