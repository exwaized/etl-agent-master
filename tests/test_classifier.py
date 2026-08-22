import pytest
from agent.classifier import classify, classify_many, UNKNOWN


@pytest.mark.parametrize("error, expected", [
    # schema
    ("column 'amount' does not exist", "schema"),
    ("table users not found", "schema"),
    ("invalid type: expected int", "schema"),
    ("unexpected field: foo", "schema"),
    ("missing field: id", "schema"),
    # null
    ("NoneType object has no attribute 'strip'", "null"),
    ("missing value in row 3", "null"),
    ("not null constraint violated", "null"),
    # timeout
    ("connection timed out after 30s", "timeout"),
    ("read timeout on socket", "timeout"),
    ("deadline exceeded", "timeout"),
    # logic
    ("AssertionError: expected True", "logic"),
    ("index out of range", "logic"),
    ("KeyError: 'revenue'", "logic"),
    ("division by zero", "logic"),
    # unknown
    ("something completely different", UNKNOWN),
    ("", UNKNOWN),
])
def test_classify(error, expected):
    assert classify(error) == expected


def test_classify_case_insensitive():
    assert classify("COLUMN DOES NOT EXIST") == "schema"
    assert classify("TIMEOUT") == "timeout"


def test_classify_returns_first_match():
    # "schema" pattern appears before "logic" in RULES; message hits both
    result = classify("column assertion failed")
    assert result == "schema"


def test_classify_many_empty():
    assert classify_many([]) == []


def test_classify_many_preserves_order():
    errors = ["timeout after 10s", "null pointer", "unknown problem"]
    assert classify_many(errors) == ["timeout", "null", UNKNOWN]
