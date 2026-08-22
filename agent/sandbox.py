from dataclasses import dataclass


@dataclass
class PatchResult:
    accepted: bool
    returncode: int
    stdout: str
    stderr: str


def run_patch(code: str, timeout: int = 20) -> PatchResult:
    """
    Stage 1 pre-filter: syntax validity only.

    Previous version ran `code` as a standalone script inside Docker and
    checked exit code. That's wrong for two independent reasons:
      1. It never proves correctness -- a function def that's never called
         exits 0 regardless of what's inside it.
      2. It actively produces FALSE REJECTIONS for patches that are valid
         function-body fragments (e.g. "df = df.dropna()...") -- these
         reference variables (df, state) that only exist once substituted
         into their real context, so running them standalone always
         raises NameError, rejecting a potentially correct patch for a
         reason unrelated to its actual quality.

    compile() sidesteps both: it parses the code into a code object
    without executing it, so undefined names never matter here -- only
    genuine syntax errors (bad indentation, mismatched parens, etc.) get
    caught. No Docker, no subprocess, no context needed. Correctness is
    Stage 2's job (agent/validator.py), which runs the patch substituted
    into its real location against real data -- the only place "does this
    actually work" can be honestly answered.
    """
    try:
        compile(code, "<patch>", "exec")
        return PatchResult(accepted=True, returncode=0, stdout="", stderr="")
    except SyntaxError as exc:
        return PatchResult(
            accepted=False,
            returncode=1,
            stdout="",
            stderr=f"SyntaxError: {exc.msg} (line {exc.lineno})",
        )