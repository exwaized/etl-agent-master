import re

ErrorCategory = str

RULES: list[tuple[ErrorCategory, re.Pattern]] = [
    ("schema",  re.compile(r"schema|column|table|relation|does not exist|invalid.*type|type.*mismatch|unexpected.*field|missing.*field", re.I)),
    ("null",    re.compile(r"null|none|NoneType|missing.*value|value.*missing|not.*null.*constraint", re.I)),
    ("timeout", re.compile(r"timeout|timed? out|deadline.*exceeded|connection.*reset|read.*timeout|socket.*timeout", re.I)),
    ("logic",   re.compile(r"assertion|index.*out.*of.*range|key.*error|division.*by.*zero|overflow|underflow|invalid.*state|unexpected.*value", re.I)),
]

UNKNOWN: ErrorCategory = "unknown"


def classify(error: str) -> ErrorCategory:
    for category, pattern in RULES:
        if pattern.search(error):
            return category
    return UNKNOWN


def classify_many(errors: list[str]) -> list[ErrorCategory]:
    return [classify(e) for e in errors]
