import json
import io
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.cli import serve
from src.database import Database


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT_SECONDS = 12

PAUSE_DURING_SERVE_WRITE = r"""
import sys
import src.storage as storage
from src.cli import main

original_write = storage.os.write
paused = False

def pause_after_partial_write(fd, data):
    global paused
    if not paused:
        paused = True
        data = bytes(data)
        amount = max(1, len(data) // 2)
        written = original_write(fd, data[:amount])
        sys.stderr.write("PARTIAL_RECORD_WRITTEN\n")
        sys.stderr.flush()
        sys.stdin.readline()
        return written + original_write(fd, data[written:])
    return original_write(fd, data)

storage.os.write = pause_after_partial_write
raise SystemExit(main(["serve", sys.argv[1]]))
"""

PAUSE_DURING_RECOVERY = r"""
import sys
import src.database as database_module

original_read_records = database_module.read_records

def pause_after_recovery_scan(path):
    records = original_read_records(path)
    sys.stderr.write("RECOVERY_SCAN_COMPLETE\n")
    sys.stderr.flush()
    sys.stdin.readline()
    return records

database_module.read_records = pause_after_recovery_scan
database_module.Database(sys.argv[1])
"""

PAUSE_DURING_COMPACTION = r"""
import os
import sys
import src.storage as storage
from src.database import Database

original_replace = storage.os.replace
original_encode_record = storage.encode_record

def pause():
    sys.stderr.write("COMPACTION_PAUSED\n")
    sys.stderr.flush()
    sys.stdin.readline()

def pause_during_write(record_type, payload):
    record = original_encode_record(record_type, payload)
    pause()
    return record

def pause_at_replace(source, destination):
    sys.stderr.write("COMPACTION_READY_TO_REPLACE\n")
    sys.stderr.flush()
    sys.stdin.readline()
    original_replace(source, destination)
    sys.stderr.write("COMPACTION_REPLACED\n")
    sys.stderr.flush()
    sys.stdin.readline()

if sys.argv[2] == "write":
    storage.encode_record = pause_during_write
else:
    storage.os.replace = pause_at_replace
with Database(sys.argv[1]) as database:
    database.compact()
"""

PAUSE_BEFORE_PARTIAL_ENOSPC = r"""
import errno
import sys
import src.storage as storage
from src.cli import main

original_write = storage.os.write
failed = False

def partial_then_enospc(fd, data):
    global failed
    if not failed:
        failed = True
        data = bytes(data)
        written = original_write(fd, data[:max(1, len(data) // 2)])
        sys.stderr.write("PARTIAL_WRITE_BEFORE_ENOSPC\n")
        sys.stderr.flush()
        raise OSError(errno.ENOSPC, "simulated disk full")
    return original_write(fd, data)

storage.os.write = partial_then_enospc
raise SystemExit(main(["serve", sys.argv[1]]))
"""


def b64(value):
    import base64
    return base64.b64encode(value).decode("ascii")


def json_request(request):
    return json.dumps(request) + "\n"


class PausedProcess:
    def __init__(self, process):
        self.process = process
        self.stderr_messages = queue.Queue()
        self.stderr_reader = threading.Thread(
            target=self._read_stderr,
            daemon=True,
        )
        self.stderr_reader.start()

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr_messages.put(line)
        self.stderr_messages.put(None)

    def read_stderr(self, timeout=TIMEOUT_SECONDS):
        try:
            line = self.stderr_messages.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                "Timed out waiting for child event; status={}".format(
                    self.process.poll()
                )
            )
        if line is None:
            raise AssertionError(
                "Child exited before pause; status={}".format(
                    self.process.poll()
                )
            )
        return line.rstrip("\r\n")

    def force_kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=TIMEOUT_SECONDS)
        if self.process.stdout:
            self.process.stdout.close()
        if self.process.stdin:
            self.process.stdin.close()
        self.stderr_reader.join(timeout=TIMEOUT_SECONDS)
        if self.process.stderr:
            self.process.stderr.close()


class TortureScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = os.path.join(
            self.temporary_directory.name, "torture.kv"
        )

    def start_paused_process(self, script):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = (
            PROJECT_ROOT
            if not environment.get("PYTHONPATH")
            else PROJECT_ROOT + os.pathsep + environment["PYTHONPATH"]
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script, self.database_path],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        paused_process = PausedProcess(process)
        self.addCleanup(paused_process.force_kill)
        return paused_process

    def start_compaction_process(self, phase):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = (
            PROJECT_ROOT
            if not environment.get("PYTHONPATH")
            else PROJECT_ROOT + os.pathsep + environment["PYTHONPATH"]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                PAUSE_DURING_COMPACTION,
                self.database_path,
                phase,
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        paused_process = PausedProcess(process)
        self.addCleanup(paused_process.force_kill)
        return paused_process

    def run_fresh_database(self, code, timeout=TIMEOUT_SECONDS):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = (
            PROJECT_ROOT
            if not environment.get("PYTHONPATH")
            else PROJECT_ROOT + os.pathsep + environment["PYTHONPATH"]
        )
        return subprocess.run(
            [sys.executable, "-c", code, self.database_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )

    def test_force_kill_immediately_after_put_ack_preserves_value(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = (
            PROJECT_ROOT
            if not environment.get("PYTHONPATH")
            else PROJECT_ROOT + os.pathsep + environment["PYTHONPATH"]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "src.cli",
                "serve",
                self.database_path,
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self.addCleanup(self._kill_and_close, process)

        process.stdin.write(json_request({
            "id": "after-ack",
            "op": "put",
            "key": b64(b"ack-key"),
            "value": b64(b"ack-value"),
        }))
        process.stdin.flush()
        response = process.stdout.readline()
        self.assertEqual(response, "ACK after-ack\n")

        process.kill()
        process.wait(timeout=TIMEOUT_SECONDS)
        with Database(self.database_path) as database:
            self.assertEqual(database.get(b"ack-key"), b"ack-value")

    @staticmethod
    def _kill_and_close(process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT_SECONDS)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()

    def test_force_kill_during_partial_put_recovers_previous_value(self):
        with Database(self.database_path) as database:
            database.put(b"key", b"old")

        process = self.start_paused_process(PAUSE_DURING_SERVE_WRITE)
        process.process.stdin.write(json_request({
            "id": "mid-put",
            "op": "put",
            "key": b64(b"key"),
            "value": b64(b"new"),
        }))
        process.process.stdin.flush()
        self.assertEqual(process.read_stderr(), "PARTIAL_RECORD_WRITTEN")
        process.force_kill()

        with Database(self.database_path) as recovered:
            self.assertEqual(recovered.get(b"key"), b"old")

    def test_force_kill_during_partial_batch_keeps_entire_old_state(self):
        with Database(self.database_path) as database:
            database.put(b"first", b"old-first")
            database.put(b"second", b"old-second")

        process = self.start_paused_process(PAUSE_DURING_SERVE_WRITE)
        process.process.stdin.write(json_request({
            "id": "mid-batch",
            "op": "batch",
            "operations": [
                {"op": "put", "key": b64(b"first"), "value": b64(b"new-first")},
                {"op": "delete", "key": b64(b"second")},
            ],
        }))
        process.process.stdin.flush()
        self.assertEqual(process.read_stderr(), "PARTIAL_RECORD_WRITTEN")
        process.force_kill()

        with Database(self.database_path) as recovered:
            self.assertEqual(recovered.get(b"first"), b"old-first")
            self.assertEqual(recovered.get(b"second"), b"old-second")

    def test_one_thousand_same_key_updates_recover_latest_value(self):
        with Database(self.database_path) as database:
            for index in range(1000):
                database.put(b"same", str(index).encode("ascii"))
            self.assertEqual(database.get(b"same"), b"999")

        with Database(self.database_path) as reopened:
            self.assertEqual(reopened.get(b"same"), b"999")

    def test_deleting_nonexistent_key_returns_false_without_log_write(self):
        with Database(self.database_path) as database:
            database.put(b"present", b"value")
            with open(self.database_path, "rb") as file:
                before = file.read()

            self.assertFalse(database.delete(b"absent"))

            with open(self.database_path, "rb") as file:
                after = file.read()
        self.assertEqual(after, before)

    def test_engine_accepts_10000_batch_operations_and_rejects_10001(self):
        operations = [("delete", b"key")] * 10000
        with Database(self.database_path) as database:
            self.assertIs(database.batch(operations), True)
            with open(self.database_path, "rb") as file:
                committed_bytes = file.read()

            with self.assertRaisesRegex(ValueError, "Too many batch operations"):
                database.batch(operations + [("delete", b"key")])

            with open(self.database_path, "rb") as file:
                self.assertEqual(file.read(), committed_bytes)

        with Database(self.database_path) as reopened:
            self.assertEqual(reopened.key_count(), 0)

    def test_maximum_one_mib_value_survives_restart(self):
        value = b"v" * (1024 * 1024)
        with Database(self.database_path) as database:
            database.put(b"maximum", value)

        with Database(self.database_path) as reopened:
            self.assertEqual(reopened.get(b"maximum"), value)

    def test_disk_full_fault_injection_returns_error_without_ack(self):
        request = json_request({
            "id": "disk-full",
            "op": "put",
            "key": b64(b"key"),
            "value": b64(b"value"),
        })
        output = io.StringIO()
        errors = io.StringIO()

        with patch(
            "src.database.append_record",
            side_effect=OSError("simulated disk full"),
        ):
            exit_code = serve(
                self.database_path,
                input_stream=io.StringIO(request),
                output_stream=output,
                error_stream=errors,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "ERR disk-full simulated disk full\n")
        self.assertEqual(errors.getvalue(), "")
        with Database(self.database_path) as reopened:
            self.assertIsNone(reopened.get(b"key"))

    def test_kill_during_recovery_leaves_recoverable_database(self):
        with Database(self.database_path) as database:
            database.put(b"saved", b"value")
        with open(self.database_path, "ab") as file:
            file.write(b"KV")

        process = self.start_paused_process(PAUSE_DURING_RECOVERY)
        self.assertEqual(process.read_stderr(), "RECOVERY_SCAN_COMPLETE")
        process.force_kill()

        with Database(self.database_path) as recovered:
            self.assertEqual(recovered.get(b"saved"), b"value")
        with open(self.database_path, "rb") as file:
            contents = file.read()
        self.assertFalse(contents.endswith(b"KV"))

    def test_kill_before_compaction_replace_keeps_original_database(self):
        with Database(self.database_path) as database:
            database.put(b"updated", b"old" * 100)
            database.put(b"removed", b"gone")
            database.put(b"updated", b"new")
            database.delete(b"removed")
            expected = {b"updated": b"new"}

        process = self.start_compaction_process("before")
        self.assertEqual(process.read_stderr(), "COMPACTION_READY_TO_REPLACE")
        process.force_kill()

        with Database(self.database_path) as recovered:
            self.assertEqual(dict(recovered.items()), expected)
            self.assertIs(recovered.check(), True)

    def test_kill_after_compaction_replace_keeps_compacted_database(self):
        with Database(self.database_path) as database:
            database.put(b"updated", b"old" * 100)
            database.put(b"removed", b"gone")
            database.put(b"updated", b"new")
            database.delete(b"removed")
            expected = {b"updated": b"new"}

        size_before = os.path.getsize(self.database_path)
        process = self.start_compaction_process("after")
        self.assertEqual(process.read_stderr(), "COMPACTION_READY_TO_REPLACE")
        process.process.stdin.write("\n")
        process.process.stdin.flush()
        self.assertEqual(process.read_stderr(), "COMPACTION_REPLACED")
        process.force_kill()

        self.assertLess(os.path.getsize(self.database_path), size_before)
        with Database(self.database_path) as recovered:
            self.assertEqual(dict(recovered.items()), expected)
            self.assertIs(recovered.check(), True)

    def test_kill_during_compaction_snapshot_write_keeps_original_database(self):
        with Database(self.database_path) as database:
            database.put(b"updated", b"old" * 100)
            database.put(b"removed", b"gone")
            database.put(b"updated", b"new")
            database.delete(b"removed")
            expected = {b"updated": b"new"}

        original_size = os.path.getsize(self.database_path)
        process = self.start_compaction_process("write")
        self.assertEqual(process.read_stderr(), "COMPACTION_PAUSED")
        process.force_kill()

        self.assertEqual(os.path.getsize(self.database_path), original_size)
        with Database(self.database_path) as recovered:
            self.assertEqual(dict(recovered.items()), expected)
            self.assertIs(recovered.check(), True)

    def test_partial_enospc_returns_err_and_recovers_committed_state(self):
        with Database(self.database_path) as database:
            database.put(b"committed", b"before")

        process = self.start_paused_process(PAUSE_BEFORE_PARTIAL_ENOSPC)
        process.process.stdin.write(json_request({
            "id": "disk-full-partial",
            "op": "put",
            "key": b64(b"uncommitted"),
            "value": b64(b"value"),
        }))
        process.process.stdin.flush()
        self.assertEqual(process.read_stderr(), "PARTIAL_WRITE_BEFORE_ENOSPC")
        self.assertEqual(
            process.process.stdout.readline(),
            "ERR disk-full-partial [Errno 28] simulated disk full\n",
        )
        process.force_kill()

        with Database(self.database_path) as recovered:
            self.assertEqual(recovered.get(b"committed"), b"before")
            self.assertIsNone(recovered.get(b"uncommitted"))


if __name__ == "__main__":
    unittest.main()
