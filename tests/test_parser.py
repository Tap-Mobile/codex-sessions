import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def _load_mod():
    repo_root = Path(__file__).resolve().parents[1]
    mod_path = repo_root / "codex_sessions.py"
    spec = importlib.util.spec_from_file_location("codex_sessions", mod_path)
    assert spec and spec.loader, "failed to load codex_sessions.py"
    mod = importlib.util.module_from_spec(spec)
    # Some stdlib (e.g., dataclasses) expects the module to be present in sys.modules
    # during execution.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


class TestParser(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mod()

    def _write_session(self, lines):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "rollout-test.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        return p

    def test_indexes_function_call_and_output(self):
        # Failing test until parser indexes tool calls and their outputs.
        lines = [
            {
                "timestamp": "2026-02-06T10:12:19.305Z",
                "type": "session_meta",
                "payload": {
                    "id": "sid-1",
                    "timestamp": "2026-02-06T10:12:19.286Z",
                    "cwd": "/tmp",
                    "cli_version": "0.98.0",
                },
            },
            {
                "timestamp": "2026-02-06T10:12:20.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "search for foo.py"}],
                },
            },
            {
                "timestamp": "2026-02-06T10:12:21.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "rg -n foo.py src", "yield_time_ms": 1000}),
                    "call_id": "call_1",
                },
            },
            {
                "timestamp": "2026-02-06T10:12:22.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "src/foo.py:1:print('hi')",
                },
            },
        ]
        p = self._write_session(lines)
        doc = self.mod.parse_codex_session_file(p)
        self.assertIsNotNone(doc)
        self.assertIn("rg -n foo.py src", doc.content)
        self.assertIn("src/foo.py:1:print('hi')", doc.content)

    def test_indexes_custom_tool_call_and_output(self):
        # Failing test until parser indexes custom_tool_call + output (e.g., apply_patch).
        lines = [
            {
                "timestamp": "2026-02-06T10:12:19.305Z",
                "type": "session_meta",
                "payload": {
                    "id": "sid-2",
                    "timestamp": "2026-02-06T10:12:19.286Z",
                    "cwd": "/tmp",
                    "cli_version": "0.98.0",
                },
            },
            {
                "timestamp": "2026-02-06T10:12:21.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": "call_2",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** Update File: README.md\n@@\n-Old\n+New\n*** End Patch",
                },
            },
            {
                "timestamp": "2026-02-06T10:12:22.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_2",
                    "output": json.dumps(
                        {
                            "output": "Success. Updated the following files:\nM README.md\n",
                            "metadata": {"exit_code": 0},
                        }
                    ),
                },
            },
        ]
        p = self._write_session(lines)
        doc = self.mod.parse_codex_session_file(p)
        self.assertIsNotNone(doc)
        self.assertIn("Update File: README.md", doc.content)
        self.assertIn("Success. Updated the following files", doc.content)


class TestQueryBuilder(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mod()

    def test_build_prefix_query_splits_on_punctuation(self):
        q = "TaskModal.tsx /Users/alice/foo-bar"
        built = self.mod.build_prefix_query(q)
        self.assertIn("TaskModal*", built)
        self.assertIn("tsx*", built)
        self.assertIn("Users*", built)
        self.assertIn("alice*", built)
        self.assertIn("foo*", built)
        self.assertIn("bar*", built)


class TestPreviewHelpers(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mod()

    def test_preview_build_render_lines_wrap(self):
        raw = ["abcde", "", "xy"]
        rendered, raw_to_render = self.mod._preview_build_render_lines(raw, width=2, wrap=True, x_offset=0)
        self.assertEqual(rendered, ["ab", "cd", "e", "", "xy"])
        self.assertEqual(raw_to_render, [0, 3, 4])

    def test_preview_build_render_lines_no_wrap_with_x_offset(self):
        raw = ["abcdef"]
        rendered, raw_to_render = self.mod._preview_build_render_lines(raw, width=4, wrap=False, x_offset=1)
        self.assertEqual(rendered, ["bcde"])
        self.assertEqual(raw_to_render, [0])

    def test_preview_find_matches_prefers_all_terms(self):
        lines = ["hello world", "foo bar baz", "bar only"]
        matches = self.mod._preview_find_matches(lines, ["foo", "bar"])
        self.assertEqual(matches[0][0], 1)  # line with both terms


class TestQueryInput(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mod()

    def test_apply_query_key_appends_when_not_allow_cursor_move(self):
        q, cur, handled, changed = self.mod._apply_query_key("ab", 0, ord("c"), allow_cursor_move=False)
        self.assertEqual(q, "abc")
        self.assertEqual(cur, 3)
        self.assertTrue(handled)
        self.assertTrue(changed)

    def test_apply_query_key_backspace_deletes_from_end_when_not_allow_cursor_move(self):
        q, cur, handled, changed = self.mod._apply_query_key("abc", 0, 127, allow_cursor_move=False)
        self.assertEqual(q, "ab")
        self.assertEqual(cur, 2)
        self.assertTrue(handled)
        self.assertTrue(changed)

    def test_apply_query_key_left_not_handled_when_not_allow_cursor_move(self):
        q, cur, handled, changed = self.mod._apply_query_key(
            "abc", 1, self.mod.curses.KEY_LEFT, allow_cursor_move=False
        )
        self.assertEqual((q, cur), ("abc", 1))
        self.assertFalse(handled)
        self.assertFalse(changed)

    def test_apply_query_key_left_moves_cursor_when_allow_cursor_move(self):
        q, cur, handled, changed = self.mod._apply_query_key("abc", 2, self.mod.curses.KEY_LEFT, allow_cursor_move=True)
        self.assertEqual((q, cur), ("abc", 1))
        self.assertTrue(handled)
        self.assertFalse(changed)

    def test_apply_query_key_ctrl_u_clears(self):
        q, cur, handled, changed = self.mod._apply_query_key("abc", 3, 21, allow_cursor_move=False)
        self.assertEqual((q, cur), ("", 0))
        self.assertTrue(handled)
        self.assertTrue(changed)


class TestSearchSessions(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mod()

    def test_search_sessions_can_disable_snippet(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "test.db"

        conn = self.mod.connect_db(db_path)
        with conn:
            conn.execute(
                "INSERT INTO sessions(session_id, created_at, updated_at, cwd, cli_version, file_path, title, preview) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                ("sid-1", 1, 2, "/tmp", "0.0.0", "/tmp/rollout.jsonl", "hello", "preview"),
            )
            conn.execute(
                "INSERT INTO session_fts(session_id, content) VALUES(?, ?)",
                ("sid-1", "user: hello foo\n\nassistant: bar"),
            )
        conn.close()

        rows = self.mod.search_sessions(db_path, "foo*", 10, include_snippet=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, "sid-1")
        self.assertEqual(rows[0].snippet, "")


class TestDebounce(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mod()

    def test_debounce_due_after_delay(self):
        d = self.mod._Debounce(0.1)
        d.mark(0.0)
        self.assertTrue(d.pending)
        self.assertFalse(d.due(0.05))
        self.assertTrue(d.due(0.11))
        d.clear()
        self.assertFalse(d.pending)
        self.assertFalse(d.due(1.0))

    def test_debounce_resets_when_marked_again(self):
        d = self.mod._Debounce(0.1)
        d.mark(0.0)
        d.mark(0.06)
        self.assertFalse(d.due(0.15))
        self.assertTrue(d.due(0.17))


if __name__ == "__main__":
    unittest.main()
