# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import tempfile
import shutil
import threading
import unittest
from pathlib import Path

import pytest

from colab_cli.history import HistoryLogger


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.logger = HistoryLogger(log_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_log_and_get_history(self):
        self.logger.log_event("test-session", "session_created", {"variant": "DEFAULT"})
        self.logger.log_event(
            "test-session", "execution", {"code": "print(1)", "outputs": []}
        )

        history = self.logger.get_history("test-session")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["event_type"], "session_created")
        self.assertEqual(history[1]["event_type"], "execution")
        self.assertEqual(history[1]["code"], "print(1)")

    def test_list_sessions(self):
        self.logger.log_event("s1", "event", {})
        self.logger.log_event("s2", "event", {})

        sessions = self.logger.list_sessions()
        self.assertIn("s1", sessions)
        self.assertIn("s2", sessions)
        self.assertEqual(len(sessions), 2)

    def test_reserved_event_fields_cannot_be_overridden(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.logger.log_event("s1", "event", {"event_type": "forged"})

        with self.assertRaisesRegex(ValueError, "reserved"):
            self.logger.log_event("s1", "event", {"timestamp": "forged"})

    def test_session_path_traversal_is_rejected(self):
        for name in ("../escape", "nested/name", r"nested\\name", ".", ".."):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "session name"):
                    self.logger.log_event(name, "event", {})

    def test_concurrent_events_remain_complete_json_lines(self):
        def writer(prefix):
            for index in range(40):
                self.logger.log_event(
                    "shared",
                    "event",
                    {"writer": prefix, "index": index, "payload": "x" * 4096},
                )

        threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        path = Path(self.test_dir) / "shared.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 80)
        self.assertTrue(
            all(json.loads(line)["event_type"] == "event" for line in lines)
        )

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_history_and_lock_files_are_private(self):
        self.logger.log_event("s1", "event", {})
        path = Path(self.test_dir) / "s1.jsonl"
        lock = Path(self.test_dir) / "s1.jsonl.lock"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)


def test_history_rejects_symlink(tmp_path):
    logger = HistoryLogger(log_dir=str(tmp_path))
    target = tmp_path / "target"
    target.write_text("sentinel", encoding="utf-8")
    (tmp_path / "s.jsonl").symlink_to(target)

    with pytest.raises((ValueError, RuntimeError), match="unsafe|symbolic|symlink"):
        logger.log_event("s", "event", {})

    assert target.read_text(encoding="utf-8") == "sentinel"


if __name__ == "__main__":
    unittest.main()
