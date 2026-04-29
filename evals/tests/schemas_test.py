import pytest
from evals.schemas import (
    validate_framework_manifest,
    validate_case_manifest,
    validate_envelope,
    validate_agent_output,
)


def test_valid_framework_manifest_passes():
    manifest = {
        "entry": "main.py",
        "env": [],
        "model": "gpt-4",
    }
    assert validate_framework_manifest(manifest) == []


def test_framework_manifest_missing_entry_fails():
    manifest = {
        "env": [],
        "model": "gpt-4",
    }
    errors = validate_framework_manifest(manifest)
    assert len(errors) > 0
    assert any("entry" in e for e in errors)


def test_framework_manifest_extra_key_fails():
    manifest = {
        "entry": "main.py",
        "env": [],
        "model": "gpt-4",
        "foo": "bar",
    }
    errors = validate_framework_manifest(manifest)
    assert len(errors) > 0
    assert any("foo" in e for e in errors)


def test_valid_case_manifest_passes():
    manifest = {
        "case_id": "test_case_1",
        "fixture_repo": "https://github.com/example/repo",
        "failing_test_command": "pytest test.py",
        "failure_output_path": "/path/to/output",
    }
    assert validate_case_manifest(manifest) == []


@pytest.mark.parametrize("case_id", ["nested/case", "../escape", "/tmp/escape", ".", ".."])
def test_case_manifest_rejects_path_like_case_ids(case_id):
    manifest = {
        "case_id": case_id,
        "fixture_repo": "fixtures/test_case_1",
        "failing_test_command": "pytest test.py",
        "failure_output": "",
    }

    errors = validate_case_manifest(manifest)

    assert any("case_id" in error for error in errors)


def test_case_manifest_both_failure_outputs_fails():
    manifest = {
        "case_id": "test_case_1",
        "fixture_repo": "https://github.com/example/repo",
        "failing_test_command": "pytest test.py",
        "failure_output": "some output",
        "failure_output_path": "/path/to/output",
    }
    errors = validate_case_manifest(manifest)
    assert len(errors) > 0


def test_case_manifest_neither_failure_output_fails():
    manifest = {
        "case_id": "test_case_1",
        "fixture_repo": "https://github.com/example/repo",
        "failing_test_command": "pytest test.py",
    }
    errors = validate_case_manifest(manifest)
    assert len(errors) > 0


def test_valid_envelope_passes():
    envelope = {
        "task_id": "task_123",
        "trace": {
            "steps": [],
            "tokens": {"input": 10, "output": 20},
            "latency_ms": 100,
        },
        "error": None,
        "output": None,
    }
    assert validate_envelope(envelope) == []


def test_envelope_missing_trace_fails():
    envelope = {
        "task_id": "task_123",
        "error": None,
        "output": None,
    }
    errors = validate_envelope(envelope)
    assert len(errors) > 0
    assert any("trace" in e for e in errors)


def test_agent_output_with_fixed_key_fails():
    output = {
        "root_cause": "bug in logic",
        "summary": "fixed the bug",
        "changed_files": [],
        "tests_run": [],
        "evidence": "test passed",
        "confidence": 0.9,
        "fixed": True,
    }
    errors = validate_agent_output(output)
    assert len(errors) > 0
    assert any("fixed" in e for e in errors)


def test_agent_output_with_status_fails():
    output = {
        "root_cause": "bug in logic",
        "summary": "fixed the bug",
        "changed_files": [],
        "tests_run": [],
        "evidence": "test passed",
        "confidence": 0.9,
        "status": "complete",
    }
    errors = validate_agent_output(output)
    assert len(errors) > 0
    assert any("status" in e for e in errors)


def test_agent_output_confidence_out_of_range_fails():
    output = {
        "root_cause": "bug in logic",
        "summary": "fixed the bug",
        "changed_files": [],
        "tests_run": [],
        "evidence": "test passed",
        "confidence": 1.5,
    }
    errors = validate_agent_output(output)
    assert len(errors) > 0


@pytest.mark.parametrize(
    "validator,payload,expected_error",
    [
        (
            validate_case_manifest,
            {
                "case_id": "test_case_1",
                "fixture_repo": "https://github.com/example/repo",
                "failing_test_command": "pytest test.py",
                "failure_output": "some output",
                "edit_constraints": {"max_changed_files": True},
            },
            "edit_constraints.max_changed_files must be an int",
        ),
        (
            validate_envelope,
            {
                "task_id": "task_123",
                "trace": {
                    "steps": [],
                    "tokens": {"input": True, "output": 20},
                    "latency_ms": 100,
                },
                "error": None,
                "output": None,
            },
            "trace.tokens.input and output must be ints",
        ),
        (
            validate_envelope,
            {
                "task_id": "task_123",
                "trace": {
                    "steps": [],
                    "tokens": {"input": 10, "output": False},
                    "latency_ms": 100,
                },
                "error": None,
                "output": None,
            },
            "trace.tokens.input and output must be ints",
        ),
        (
            validate_envelope,
            {
                "task_id": "task_123",
                "trace": {
                    "steps": [],
                    "tokens": {"input": 10, "output": 20},
                    "latency_ms": True,
                },
                "error": None,
                "output": None,
            },
            "trace.latency_ms must be an int",
        ),
        (
            validate_agent_output,
            {
                "root_cause": "bug in logic",
                "summary": "fixed the bug",
                "changed_files": [],
                "tests_run": [
                    {"command": "pytest", "exit_code": False, "summary": "failed"}
                ],
                "evidence": "test failed",
                "confidence": 0.9,
            },
            "tests_run[0].exit_code must be an int",
        ),
        (
            validate_agent_output,
            {
                "root_cause": "bug in logic",
                "summary": "fixed the bug",
                "changed_files": [],
                "tests_run": [],
                "evidence": "test passed",
                "confidence": True,
            },
            "confidence must be a number",
        ),
    ],
)
def test_schema_validators_reject_json_booleans_for_numeric_fields(
    validator, payload, expected_error
):
    errors = validator(payload)

    assert expected_error in errors
