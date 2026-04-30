"""Framework-local regression tests for the Pydantic-AI adapter.

Runnable from `frameworks/pydantic-ai/` via:
    uv run --quiet python -m unittest -q

Stdlib-only (unittest); no pytest dependency added to this framework.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import adapter  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)


class _AdapterStateMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self._saved_state = dict(adapter._STATE)
        self._saved_constraints = dict(adapter._EDIT_CONSTRAINTS)
        self._saved_changed = set(adapter._CHANGED_FILES)
        adapter._STATE.clear()
        adapter._STATE["repo_path"] = self.repo
        adapter._EDIT_CONSTRAINTS.clear()
        adapter._CHANGED_FILES.clear()

    def tearDown(self) -> None:
        adapter._STATE.clear()
        adapter._STATE.update(self._saved_state)
        adapter._EDIT_CONSTRAINTS.clear()
        adapter._EDIT_CONSTRAINTS.update(self._saved_constraints)
        adapter._CHANGED_FILES.clear()
        adapter._CHANGED_FILES.update(self._saved_changed)


class PathContainmentTest(_AdapterStateMixin):
    def test_resolve_within_rejects_outside_path(self) -> None:
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("nope")
        result = adapter.read_file(str(outside))
        self.assertTrue(result.startswith("error:"), result)

    def test_resolve_within_rejects_dotdot_traversal(self) -> None:
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("nope")
        result = adapter.read_file("../outside.txt")
        self.assertTrue(result.startswith("error:"), result)


class GlobAbsolutePatternTest(_AdapterStateMixin):
    def test_absolute_pattern_returns_clean_error(self) -> None:
        # Pre-fix: Path.glob raises NotImplementedError on absolute patterns,
        # surfacing as a tool exception instead of a clean `error:` string.
        result = adapter.glob("/etc/*.conf")
        self.assertIsInstance(result, str)
        self.assertTrue(
            result.startswith("error:"),
            msg=f"absolute glob should return error string, got: {result!r}",
        )


class EditConstraintsTest(_AdapterStateMixin):
    def test_write_file_rejects_disallowed_path(self) -> None:
        adapter._EDIT_CONSTRAINTS.update({
            "disallowed_paths": ["tests/**", "**/*lock*"],
            "max_changed_files": 5,
        })
        result = adapter.write_file("tests/test_foo.py", "x")
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("disallowed", result)
        self.assertFalse((self.repo / "tests" / "test_foo.py").exists())

        result = adapter.write_file("uv.lock", "x")
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("disallowed", result)

    def test_write_file_rejects_outside_allowed_paths(self) -> None:
        adapter._EDIT_CONSTRAINTS.update({
            "disallowed_paths": [],
            "allowed_paths": ["src/**"],
            "max_changed_files": 5,
        })
        result = adapter.write_file("README.md", "x")
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("not allowed", result)
        self.assertFalse((self.repo / "README.md").exists())

    def test_write_file_allows_path_inside_allowed(self) -> None:
        adapter._EDIT_CONSTRAINTS.update({
            "disallowed_paths": [],
            "allowed_paths": ["src/**"],
            "max_changed_files": 5,
        })
        result = adapter.write_file("src/foo.py", "x = 1\n")
        self.assertTrue(result.startswith("ok:"), result)
        self.assertEqual((self.repo / "src" / "foo.py").read_text(), "x = 1\n")

    def test_write_file_enforces_max_changed_files(self) -> None:
        adapter._EDIT_CONSTRAINTS.update({
            "disallowed_paths": [],
            "max_changed_files": 2,
        })
        self.assertTrue(adapter.write_file("a.py", "1").startswith("ok:"))
        self.assertTrue(adapter.write_file("b.py", "2").startswith("ok:"))
        # Third distinct file would push count to 3 — must reject.
        result = adapter.write_file("c.py", "3")
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("max_changed_files", result)
        self.assertFalse((self.repo / "c.py").exists())
        # Re-writing an already-tracked file does not exceed the cap.
        again = adapter.write_file("a.py", "1-updated")
        self.assertTrue(again.startswith("ok:"), again)

    def test_edit_file_enforces_disallowed(self) -> None:
        # Seed an existing file outside constraints to edit.
        target = self.repo / "tests" / "test_foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("hello world")
        adapter._EDIT_CONSTRAINTS.update({
            "disallowed_paths": ["tests/**"],
            "max_changed_files": 5,
        })
        result = adapter.edit_file("tests/test_foo.py", "hello", "goodbye")
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("disallowed", result)
        self.assertEqual(target.read_text(), "hello world")

    def test_edit_file_enforces_max_changed_files(self) -> None:
        # Two distinct files already in the changed set; editing a third should fail.
        adapter._EDIT_CONSTRAINTS.update({
            "disallowed_paths": [],
            "max_changed_files": 2,
        })
        for name in ("a.py", "b.py", "c.py"):
            (self.repo / name).write_text("x")
        adapter._CHANGED_FILES.update({"a.py", "b.py"})
        result = adapter.edit_file("c.py", "x", "y")
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("max_changed_files", result)


class RunShellEnvTest(_AdapterStateMixin):
    def test_run_shell_passes_case_venv_env(self) -> None:
        case_venv = Path(self._tmp.name) / "case-venv"
        (case_venv / "bin").mkdir(parents=True)
        captured: dict = {}

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")

            class _R:
                returncode = 0
                stdout = b""
                stderr = b""

            return _R()

        env_patch = {
            "AGENT_HARNESS_CASE_VENV": str(case_venv),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp/fake-home",
            "LANG": "en_US.UTF-8",
            "ANTHROPIC_API_KEY": "sk-ant-SENSITIVE",
        }
        with mock.patch.dict(os.environ, env_patch, clear=True), \
                mock.patch.object(adapter.subprocess, "run", side_effect=fake_run):
            adapter.run_shell("echo hi")

        env = captured["env"]
        self.assertIsNotNone(env)
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(case_venv.resolve()))
        self.assertEqual(env["UV_NO_SYNC"], "1")
        # PATH must have the case-venv bin first.
        self.assertTrue(
            env["PATH"].startswith(str(case_venv.resolve() / "bin")),
            f"PATH should start with case-venv bin: {env['PATH']!r}",
        )
        # PYTHONPATH must include repo and repo/src to mirror build_test_env.
        pp = env.get("PYTHONPATH", "")
        self.assertIn(str(self.repo.resolve()), pp)
        self.assertIn(str(self.repo.resolve() / "src"), pp)
        # Provider secrets must NOT leak to model-controlled shell.
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(captured["cwd"], str(self.repo))

    def test_run_shell_works_without_case_venv(self) -> None:
        captured: dict = {}

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            captured["env"] = kwargs.get("env")

            class _R:
                returncode = 0
                stdout = b""
                stderr = b""

            return _R()

        with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/h"}, clear=True), \
                mock.patch.object(adapter.subprocess, "run", side_effect=fake_run):
            adapter.run_shell("echo hi")

        env = captured["env"]
        self.assertNotIn("UV_PROJECT_ENVIRONMENT", env)
        self.assertNotIn("UV_NO_SYNC", env)
        self.assertIn(str(self.repo.resolve()), env.get("PYTHONPATH", ""))


class TraceConversionTest(unittest.TestCase):
    def test_messages_to_trace_pairs_tool_call_with_return(self) -> None:
        req = ModelRequest(parts=[])  # placeholder; we only inspect ModelResponse + ModelRequest with returns
        # ModelResponse with a tool call.
        resp = ModelResponse(parts=[
            ToolCallPart(tool_name="read_file", args='{"path": "a.py"}', tool_call_id="c1"),
        ])
        # ModelRequest carrying the tool return.
        ret = ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", tool_call_id="c1", content="hello"),
        ])
        trace = adapter._messages_to_trace([resp, ret], latency_ms=42, total_usage=None)
        steps = trace["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["kind"], "tool_call")
        self.assertEqual(steps[0]["name"], "read_file")
        self.assertEqual(steps[0]["args"], {"path": "a.py"})
        self.assertEqual(steps[0]["result"], {"content": "hello"})
        self.assertEqual(trace["latency_ms"], 42)
        self.assertEqual(trace["tokens"], {"input": 0, "output": 0})

    def test_messages_to_trace_emits_text_step(self) -> None:
        resp = ModelResponse(parts=[TextPart(content="thinking out loud")])
        trace = adapter._messages_to_trace([resp], latency_ms=1, total_usage=None)
        steps = trace["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["kind"], "model_call")
        self.assertEqual(steps[0]["name"], "text")
        self.assertEqual(steps[0]["result"], {"content": "thinking out loud"})


class InvalidRequestTest(unittest.TestCase):
    def test_main_emits_error_envelope_on_empty_stdin(self) -> None:
        stdin = io.StringIO("")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(adapter.sys, "stdin", stdin), \
                mock.patch.object(adapter.sys, "stdout", stdout), \
                mock.patch.object(adapter.sys, "stderr", stderr):
            rc = adapter.main()
        self.assertEqual(rc, 1)
        line = stdout.getvalue().strip().splitlines()[0]
        env = json.loads(line)
        self.assertEqual(env["task_id"], "unknown")
        self.assertIsNone(env["output"])
        self.assertIsNotNone(env["error"])
        self.assertIn("steps", env["trace"])
        self.assertEqual(env["trace"]["tokens"], {"input": 0, "output": 0})


if __name__ == "__main__":
    unittest.main()
