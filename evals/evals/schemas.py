import re
from typing import Any

FORBIDDEN_OUTPUT_KEYS = {"fixed", "not_fixed", "status"}
FRAMEWORK_MANIFEST_REQUIRED = {"entry", "env", "model"}
FRAMEWORK_MANIFEST_OPTIONAL = {"setup"}
CASE_REQUIRED = {"case_id", "fixture_repo", "failing_test_command"}
CASE_OPTIONAL = {"failure_output", "failure_output_path", "hidden_test_command", "edit_constraints", "notes"}
ENVELOPE_REQUIRED = {"task_id", "output", "trace", "error"}
TRACE_REQUIRED = {"steps", "tokens", "latency_ms"}
OUTPUT_REQUIRED = {"root_cause", "summary", "changed_files", "tests_run", "evidence", "confidence"}
CASE_ID_PATTERN = r"^(?!.*(?:^|/)\.{1,2}(?:/|$))[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*$"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_framework_manifest(obj: object) -> list[str]:
    errors = []

    if not isinstance(obj, dict):
        errors.append("framework manifest must be a dict")
        return errors

    # Check required keys
    missing_keys = FRAMEWORK_MANIFEST_REQUIRED - set(obj.keys())
    for key in missing_keys:
        errors.append(f"missing required key: {key}")

    # Check for unknown keys
    allowed_keys = FRAMEWORK_MANIFEST_REQUIRED | FRAMEWORK_MANIFEST_OPTIONAL
    unknown_keys = set(obj.keys()) - allowed_keys
    for key in unknown_keys:
        errors.append(f"unknown key: {key}")

    # Validate field types and values
    if "entry" in obj:
        if not isinstance(obj["entry"], str) or not obj["entry"]:
            errors.append("entry must be a non-empty string")

    if "setup" in obj:
        if not isinstance(obj["setup"], str) or not obj["setup"]:
            errors.append("setup must be a non-empty string")

    if "env" in obj:
        if not isinstance(obj["env"], list):
            errors.append("env must be a list")
        elif not all(isinstance(item, str) for item in obj["env"]):
            errors.append("env items must be strings")

    if "model" in obj:
        if not isinstance(obj["model"], str) or not obj["model"]:
            errors.append("model must be a non-empty string")

    return errors


def validate_case_manifest(obj: object) -> list[str]:
    errors = []

    if not isinstance(obj, dict):
        errors.append("case manifest must be a dict")
        return errors

    # Check required keys
    missing_keys = CASE_REQUIRED - set(obj.keys())
    for key in missing_keys:
        errors.append(f"missing required key: {key}")

    # Check for unknown keys
    allowed_keys = CASE_REQUIRED | CASE_OPTIONAL
    unknown_keys = set(obj.keys()) - allowed_keys
    for key in unknown_keys:
        errors.append(f"unknown key: {key}")

    # Validate case_id format. Case IDs may contain slash-separated segments for
    # org/repo-style identifiers, but absolute paths, empty segments, and
    # traversal segments are rejected before they are used as artifact paths.
    if "case_id" in obj:
        case_id = obj["case_id"]
        if not isinstance(case_id, str):
            errors.append("case_id must be a string")
        elif not re.fullmatch(CASE_ID_PATTERN, case_id):
            errors.append(f"case_id must match pattern {CASE_ID_PATTERN}")

    # Validate fixture_repo
    if "fixture_repo" in obj:
        if not isinstance(obj["fixture_repo"], str) or not obj["fixture_repo"]:
            errors.append("fixture_repo must be a non-empty string")

    # Validate failing_test_command
    if "failing_test_command" in obj:
        if not isinstance(obj["failing_test_command"], str) or not obj["failing_test_command"]:
            errors.append("failing_test_command must be a non-empty string")

    # Validate failure_output XOR failure_output_path
    has_failure_output = "failure_output" in obj
    has_failure_output_path = "failure_output_path" in obj
    if has_failure_output and has_failure_output_path:
        errors.append("cannot have both failure_output and failure_output_path")
    elif not has_failure_output and not has_failure_output_path:
        errors.append("must have exactly one of failure_output or failure_output_path")
    if has_failure_output and not isinstance(obj["failure_output"], str):
        errors.append("failure_output must be a string")
    if has_failure_output_path and not isinstance(obj["failure_output_path"], str):
        errors.append("failure_output_path must be a string")

    # Validate edit_constraints if present
    if "edit_constraints" in obj:
        constraints = obj["edit_constraints"]
        if not isinstance(constraints, dict):
            errors.append("edit_constraints must be a dict")
        else:
            if "disallowed_paths" in constraints:
                if not isinstance(constraints["disallowed_paths"], list):
                    errors.append("edit_constraints.disallowed_paths must be a list")
                elif not all(isinstance(item, str) for item in constraints["disallowed_paths"]):
                    errors.append("edit_constraints.disallowed_paths items must be strings")

            if "allowed_paths" in constraints:
                if not isinstance(constraints["allowed_paths"], list):
                    errors.append("edit_constraints.allowed_paths must be a list")
                elif not all(isinstance(item, str) for item in constraints["allowed_paths"]):
                    errors.append("edit_constraints.allowed_paths items must be strings")

            if "max_changed_files" in constraints:
                if not _is_int(constraints["max_changed_files"]):
                    errors.append("edit_constraints.max_changed_files must be an int")
                elif constraints["max_changed_files"] < 0:
                    errors.append("edit_constraints.max_changed_files must be non-negative")

    # Validate hidden_test_command if present
    if "hidden_test_command" in obj:
        if not isinstance(obj["hidden_test_command"], str) or not obj["hidden_test_command"]:
            errors.append("hidden_test_command must be a non-empty string")

    return errors


def validate_envelope(obj: object) -> list[str]:
    errors = []

    if not isinstance(obj, dict):
        errors.append("envelope must be a dict")
        return errors

    # Check required keys
    missing_keys = ENVELOPE_REQUIRED - set(obj.keys())
    for key in missing_keys:
        errors.append(f"missing required key: {key}")

    # Validate task_id
    if "task_id" in obj:
        if not isinstance(obj["task_id"], str) or not obj["task_id"]:
            errors.append("task_id must be a non-empty string")

    # Validate trace
    if "trace" in obj:
        trace = obj["trace"]
        if not isinstance(trace, dict):
            errors.append("trace must be a dict")
        else:
            # Check trace required keys
            missing_trace_keys = TRACE_REQUIRED - set(trace.keys())
            for key in missing_trace_keys:
                errors.append(f"trace missing required key: {key}")

            # Validate tokens
            if "tokens" in trace:
                tokens = trace["tokens"]
                if not isinstance(tokens, dict):
                    errors.append("trace.tokens must be a dict")
                elif not ("input" in tokens and "output" in tokens):
                    errors.append("trace.tokens must have input and output fields")
                elif not (_is_int(tokens["input"]) and _is_int(tokens["output"])):
                    errors.append("trace.tokens.input and output must be ints")

            # Validate steps
            if "steps" in trace:
                if not isinstance(trace["steps"], list):
                    errors.append("trace.steps must be a list")

            # Validate latency_ms
            if "latency_ms" in trace:
                latency = trace["latency_ms"]
                if not _is_int(latency):
                    errors.append("trace.latency_ms must be an int")
                elif latency < 0:
                    errors.append("trace.latency_ms must be non-negative")

    # Validate error
    if "error" in obj:
        error = obj["error"]
        if error is not None:
            if not isinstance(error, dict):
                errors.append("error must be null or a dict")
            elif "message" not in error or not isinstance(error["message"], str):
                errors.append("error dict must have a message: str field")

    # Validate output (minimal check - just that it's null or dict)
    if "output" in obj:
        output = obj["output"]
        if output is not None and not isinstance(output, dict):
            errors.append("output must be null or a dict")

    return errors


def validate_agent_output(obj: object) -> list[str]:
    errors = []

    if not isinstance(obj, dict):
        errors.append("output must be a dict")
        return errors

    # Check required keys
    missing_keys = OUTPUT_REQUIRED - set(obj.keys())
    for key in missing_keys:
        errors.append(f"missing required key: {key}")

    # Check for forbidden keys
    forbidden_found = set(obj.keys()) & FORBIDDEN_OUTPUT_KEYS
    for key in forbidden_found:
        errors.append(f"forbidden key in output: {key}")

    # Validate root_cause
    if "root_cause" in obj:
        if not isinstance(obj["root_cause"], str):
            errors.append("root_cause must be a string")

    # Validate summary
    if "summary" in obj:
        if not isinstance(obj["summary"], str):
            errors.append("summary must be a string")

    # Validate changed_files
    if "changed_files" in obj:
        files = obj["changed_files"]
        if not isinstance(files, list):
            errors.append("changed_files must be a list")
        elif not all(isinstance(item, str) for item in files):
            errors.append("changed_files items must be strings")

    # Validate tests_run
    if "tests_run" in obj:
        tests = obj["tests_run"]
        if not isinstance(tests, list):
            errors.append("tests_run must be a list")
        else:
            for i, test in enumerate(tests):
                if not isinstance(test, dict):
                    errors.append(f"tests_run[{i}] must be a dict")
                elif not all(key in test for key in ("command", "exit_code", "summary")):
                    errors.append(f"tests_run[{i}] missing required fields: command, exit_code, summary")
                else:
                    if not isinstance(test.get("command"), str):
                        errors.append(f"tests_run[{i}].command must be a string")
                    if not _is_int(test.get("exit_code")):
                        errors.append(f"tests_run[{i}].exit_code must be an int")
                    if not isinstance(test.get("summary"), str):
                        errors.append(f"tests_run[{i}].summary must be a string")

    # Validate evidence
    if "evidence" in obj:
        if not isinstance(obj["evidence"], str):
            errors.append("evidence must be a string")

    # Validate confidence
    if "confidence" in obj:
        confidence = obj["confidence"]
        if not _is_number(confidence):
            errors.append("confidence must be a number")
        elif not (0.0 <= confidence <= 1.0):
            errors.append("confidence must be in range [0.0, 1.0]")

    return errors
