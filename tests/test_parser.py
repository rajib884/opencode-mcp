"""Unit tests: JSONL parser vs real captured opencode 1.18.2 streams, guards,
session store, and the timeout kill-path (fake slow binary).

Run: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import guards
import runner
from sessions_store import SessionBusy, SessionStore

FIXTURES = Path(__file__).parent / "fixtures"


def parse_fixture(name: str) -> runner.EventAccumulator:
    acc = runner.EventAccumulator()
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        acc.feed_line(line)
    return acc


class EnvPatch:
    """Minimal env patcher (stdlib unittest has no monkeypatch)."""

    def __init__(self, **kv: str | None):
        self.kv = kv
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestParser(unittest.TestCase):
    def test_turn1_write_session(self):
        acc = parse_fixture("turn1.jsonl")
        self.assertTrue(acc.session_id and acc.session_id.startswith("ses"))
        self.assertIn("write", acc.tools_used)
        self.assertEqual(len(acc.files_changed), 1)
        self.assertTrue(acc.files_changed[0].endswith("greet.py"))
        self.assertGreater(acc.tokens["input"], 0)
        self.assertGreater(acc.tokens["output"], 0)
        self.assertEqual(acc.cost, 0)
        self.assertEqual(acc.text.strip(), "Done.")

    def test_turn2_resumed_same_session_edit_captured(self):
        a1 = parse_fixture("turn1.jsonl")
        a2 = parse_fixture("turn2.jsonl")
        self.assertEqual(a1.session_id, a2.session_id)
        self.assertIn("edit", a2.tools_used)
        self.assertTrue(any(f.endswith("greet.py") for f in a2.files_changed))

    def test_pipe_pong(self):
        acc = parse_fixture("pipe.jsonl")
        self.assertEqual(acc.text.strip(), "pong")
        self.assertEqual(acc.files_changed, [])
        self.assertEqual(acc.tools_used, [])

    def test_plan_mode_blocks_writes(self):
        acc = parse_fixture("plan.jsonl")
        self.assertEqual(acc.files_changed, [])
        self.assertEqual(acc.tools_used, [])
        self.assertIn("read-only", acc.text.lower().replace("read‑only", "read-only"))

    def test_websearch_tool_captured(self):
        acc = parse_fixture("weather-plan.jsonl")
        self.assertIn("websearch", acc.tools_used)
        self.assertEqual(acc.files_changed, [])

    def test_garbage_lines_skipped(self):
        acc = runner.EventAccumulator()
        acc.feed_line("not json at all")
        acc.feed_line('{"type": "text"}')  # no part
        acc.feed_line("[1,2,3]")  # not a dict
        acc.feed_line("")
        self.assertEqual(acc.text, "")
        self.assertEqual(acc.events, 1)  # only the parseable dict counts


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ocmcp-guards-")

    def test_relative_dir_refused(self):
        with self.assertRaises(guards.GuardError):
            guards.validate_dir("relative/path", "read")

    def test_missing_dir_refused(self):
        with self.assertRaises(guards.GuardError):
            guards.validate_dir(self.tmp + "/nope", "read")

    def test_read_mode_allows_protected(self):
        self.assertEqual(guards.validate_dir("/etc", "read"), Path("/etc"))

    def test_write_mode_refuses_protected(self):
        for bad in ("/", "/etc", str(Path.home())):
            with self.assertRaises(guards.GuardError, msg=bad):
                guards.validate_dir(bad, "write")

    @unittest.skipIf(os.name == "nt", "POSIX path")
    def test_write_mode_refuses_under_protected(self):
        if not Path("/usr/local").is_dir():
            self.skipTest("/usr/local missing")
        with self.assertRaises(guards.GuardError):
            guards.validate_dir("/usr/local", "write")

    def test_write_mode_allows_scratch(self):
        self.assertEqual(guards.validate_dir(self.tmp, "write"), Path(self.tmp).resolve())

    def test_allowlist(self):
        other = tempfile.mkdtemp(prefix="ocmcp-other-")
        inside = Path(self.tmp) / "sub"
        inside.mkdir()
        with EnvPatch(OPENCODE_MCP_ALLOWED_DIRS=self.tmp):
            guards.validate_dir(str(inside), "write")  # under an allowed root
            with self.assertRaises(guards.GuardError):
                guards.validate_dir(other, "write")
        guards.validate_dir(other, "write")  # allowlist unset again

    def test_mode_validation(self):
        self.assertEqual(guards.validate_mode("read"), "read")
        self.assertEqual(guards.validate_mode("write"), "write")
        with self.assertRaises(guards.GuardError):
            guards.validate_mode("yolo")

    def test_timeout_clamp(self):
        self.assertEqual(guards.clamp_timeout(None), guards.TIMEOUT_DEFAULT)
        self.assertEqual(guards.clamp_timeout(5), guards.TIMEOUT_MIN)
        self.assertEqual(guards.clamp_timeout(10_000), guards.TIMEOUT_MAX)
        self.assertEqual(guards.clamp_timeout(120), 120.0)
        with self.assertRaises(guards.GuardError):
            guards.clamp_timeout("soon")

    def test_opencode_config_validation(self):
        self.assertIsNone(guards.validate_opencode_config(None))
        cfg = Path(self.tmp) / "oc.json"
        cfg.write_text("{}")
        self.assertEqual(guards.validate_opencode_config(str(cfg)), str(cfg))
        with self.assertRaises(guards.GuardError):
            guards.validate_opencode_config("rel.json")
        with self.assertRaises(guards.GuardError):
            guards.validate_opencode_config(str(cfg) + ".nope")


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocmcp-store-"))
        self.store = SessionStore(self.tmp / "sessions.json")

    def test_record_accumulates(self):
        for _ in range(2):
            self.store.record_turn(
                "job", session_id="ses_x", dir="/w", mode="write",
                model="m", tokens={"input": 10, "output": 5, "reasoning": 1}, cost=0.0,
            )
        entry = self.store.get("job")
        self.assertEqual(entry["turns"], 2)
        self.assertEqual(entry["tokens_total"], {"input": 20, "output": 10, "reasoning": 2})
        self.assertEqual(entry["id"], "ses_x")
        listed = self.store.list()
        self.assertEqual(listed[0]["name"], "job")

    def test_remove(self):
        self.store.record_turn("gone", session_id="s", dir="/w", mode="read", model="m")
        self.assertTrue(self.store.remove("gone"))
        self.assertFalse(self.store.remove("gone"))
        self.assertIsNone(self.store.get("gone"))

    def test_lease_busy(self):
        with self.store.lease("busy"):
            with self.assertRaises(SessionBusy):
                with self.store.lease("busy"):
                    pass
        with self.store.lease("busy"):  # released after exit
            pass

    def test_corrupt_file_recovers(self):
        (self.tmp / "sessions.json").write_text("{corrupt")
        self.assertEqual(self.store.list(), [])
        self.store.record_turn("a", session_id="s", dir="/w", mode="read", model="m")
        self.assertEqual(len(self.store.list()), 1)


class TestRunnerProcess(unittest.TestCase):
    def test_binary_resolution_env_override(self):
        with EnvPatch(OPENCODE_MCP_BIN="/definitely/not/here"):
            with self.assertRaises(runner.BinaryNotFound):
                runner.resolve_binary()
        some_file = tempfile.NamedTemporaryFile(delete=False)
        some_file.close()
        with EnvPatch(OPENCODE_MCP_BIN=some_file.name):
            self.assertEqual(runner.resolve_binary(), some_file.name)

    @unittest.skipIf(os.name == "nt", "POSIX kill-path; Windows path exercised via e2e")
    def test_timeout_kills_process_tree(self):
        tmp = Path(tempfile.mkdtemp(prefix="ocmcp-kill-"))
        fake = tmp / "fake-opencode"
        # Spawns a child of its own so the test proves the *tree* dies.
        fake.write_text("#!/bin/sh\nsleep 300 &\nCHILD=$!\necho $CHILD > child.pid\nwait\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        result = runner.run_opencode(
            task="ignored", dir=tmp, model="m", read_only=True,
            timeout=2.0, bin_path=str(fake),
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration_ms, 30_000)
        child_pid = int((tmp / "child.pid").read_text().strip())
        for _ in range(50):  # killpg is async; give it a moment
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            import time as _t
            _t.sleep(0.1)
        else:
            self.fail(f"grandchild {child_pid} survived kill_tree")

    @unittest.skipIf(os.name == "nt", "POSIX shell fixture")
    def test_fake_stream_parsed_end_to_end(self):
        tmp = Path(tempfile.mkdtemp(prefix="ocmcp-fake-"))
        fake = tmp / "fake-opencode"
        line = (
            '{"type":"text","sessionID":"ses_fake",'
            '"part":{"type":"text","text":"hello"}}'
        )
        fake.write_text(f"#!/bin/sh\ncat > /dev/null\necho '{line}'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        result = runner.run_opencode(
            task="hi", dir=tmp, model="m", read_only=False,
            timeout=30.0, bin_path=str(fake),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.acc.text, "hello")
        self.assertEqual(result.acc.session_id, "ses_fake")


if __name__ == "__main__":
    unittest.main()
