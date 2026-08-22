import re
import time
import requests
from pydantic import BaseModel

MODEL = "llama3.2"
_OLLAMA_URL = "http://localhost:11434/api/generate"

# Deliberately NOT asking for JSON. Embedding multi-line source code as a
# JSON string value requires the model to perfectly escape newlines,
# quotes, and backslashes -- it does this reliably for short patches and
# unreliably for longer ones (exactly what broke on the aggregate() patch:
# a longer groupby/agg chain triggered escaping errors that a short
# cast_types patch happened not to). A delimited plain-text format sidesteps
# escaping entirely -- there's nothing to escape.
_SYSTEM = (
    "You are an automated pipeline debugger. You will be given a failure "
    "message, optional traceback, the current dataframe's actual columns, "
    "the exact source of the function that failed, and (on a retry) "
    "feedback on why your previous attempt was rejected.\n\n"
    "If the current dataframe's columns are provided, your fix MUST "
    "reference only columns that actually exist in that list -- do not "
    "assume or guess a column name that isn't listed.\n\n"
    "If you are given feedback about a previous rejected attempt, you MUST "
    "propose a genuinely different fix that specifically avoids the "
    "problem described -- do not repeat the same approach.\n\n"
    "If a ValueError indicates a string could not be converted to a "
    "numeric type, prefer pd.to_numeric(column, errors='coerce') over "
    "astype(float) -- astype() raises on any unparseable value, while "
    "to_numeric can convert bad values to NaN instead of crashing.\n\n"
    "Respond in EXACTLY this format and nothing else:\n\n"
    "DIAGNOSIS: <concise root-cause analysis, one line>\n"
    "CODE_PATCH:\n"
    "<the complete replacement function, from its def line through the end of its body>\n\n"
    "Rules for CODE_PATCH, all mandatory:\n"
    "1. Start with the exact same `def <name>(...)` line as the original function "
    "-- same name, same parameters, same return annotation if present.\n"
    "2. Include the ENTIRE function body -- never a bare fragment, never a diff, "
    "never just the changed lines.\n"
    "3. Do not wrap it in markdown code fences or any other formatting.\n"
    "4. Do not include anything after the code -- no explanation, no closing text."
)

_CODE_PATCH_MARKER = "CODE_PATCH:"
_DIAGNOSIS_MARKER = "DIAGNOSIS:"


class FixSuggestion(BaseModel):
    diagnosis: str
    code_patch: str


def _parse_response(raw: str) -> FixSuggestion:
    text = raw.strip()

    # Some models wrap output in code fences even when told not to -- strip
    # defensively rather than fail on a minor instruction-following miss.
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"```\s*$", "", text).strip()

    if _DIAGNOSIS_MARKER not in text or _CODE_PATCH_MARKER not in text:
        raise ValueError(f"response missing required markers: {text[:200]!r}")

    diagnosis_part, _, rest = text.partition(_CODE_PATCH_MARKER)
    diagnosis = diagnosis_part.split(_DIAGNOSIS_MARKER, 1)[1].strip()
    code_patch = rest.strip()

    if not diagnosis or not code_patch:
        raise ValueError(f"empty diagnosis or code_patch in response: {text[:200]!r}")

    return FixSuggestion(diagnosis=diagnosis, code_patch=code_patch)


def suggest_fix(
    message: str,
    context: str | None = None,
    failure_id: int | None = None,
    original_source: str | None = None,
    data_context: str | None = None,
    previous_attempt_feedback: str | None = None,
) -> FixSuggestion:
    lines = []
    if failure_id is not None:
        lines.append(f"Failure ID: {failure_id}")
    lines.append(f"Error: {message}")
    if context:
        lines.append(f"\nTraceback:\n{context}")
    if data_context:
        # Ground the fix in the ACTUAL data shape, not just the error text --
        # a KeyError on a missing column is only fixable correctly if the
        # model knows what columns actually exist to fix it TO.
        lines.append(f"\n{data_context}")
    if original_source:
        lines.append(f"\nOriginal function (replace this ENTIRE block):\n{original_source}")
    if previous_attempt_feedback:
        # Without this, every retry re-diagnoses the same original error
        # from scratch (nothing was ever persisted, so the real failure
        # never changes) and tends to propose the same KIND of fix, hitting
        # the same secondary wall every time. This is what actually gives
        # retry 2 a reason to be different from retry 1.
        lines.append(f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT: {previous_attempt_feedback}")

    joined_lines = "\n".join(lines)
    prompt = f"{_SYSTEM}\n\n{joined_lines}"

    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            response = requests.post(
                _OLLAMA_URL,
                json={
                    "model": MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_ctx": 4096},
                },
                timeout=120,
            )
            response.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            if attempt == max_attempts - 1:
                raise
            if isinstance(exc, requests.HTTPError) and exc.response.status_code < 500 and exc.response.status_code != 429:
                raise
            time.sleep(2 ** attempt)

    raw = response.json()["response"]
    return _parse_response(raw)