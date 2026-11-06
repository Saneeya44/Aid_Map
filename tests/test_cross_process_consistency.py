import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT_SECONDS = 10
WORKER_SCRIPT = r"""
import json
import sys
from src.database import Database

database = Database(sys.argv[1])
print(json.dumps({"ready": True}), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    operation = request["op"]
    try:
        if operation == "put":
            database.put(request["key"].encode(), request["value"].encode())
            response = {"ok": True}
        elif operation == "barrier_put":
            print(json.dumps({"waiting": request["id"]}), flush=True)
            if sys.stdin.readline().strip() != "GO":
                raise RuntimeError("barrier was not released")
            database.put(request["key"].encode(), request["value"].encode())
            response = {"ok": True}
        elif operation == "get":
            value = database.get(request["key"].encode())
            response = {
                "ok": True,
                "value": value.decode() if value is not None else None,
            }
        else:
            raise ValueError("unsupported worker operation")
    except Exception as error:
        response = {"ok": False, "error": str(error)}
    print(json.dumps(response), flush=True)
"""

VERIFY_SCRIPT = r"""
import json
import sys
from src.database import Database

with Database(sys.argv[1]) as database:
    result = {
        "key-A": database.get(b"key-A").decode()
            if database.get(b"key-A") is not None else None,
        "key-B": database.get(b"key-B").decode()
            if database.get(b"key-B") is not None else None,
        "same-key": database.get(b"same-key").decode()
            if database.get(b"same-key") is not None else None,
    }
print(json.dumps(result))
"""


class DatabaseWorker:
    def __init__(self, database_path):
        environment = os.environ.copy()
        current_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            PROJECT_ROOT
            if not current_pythonpath
            else PROJECT_ROOT + os.pathsep + current_pythonpath
        )
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", WORKER_SCRIPT, database_path],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self.output = queue.Queue()
        self.reader = threading.Thread(
            target=self._read_output,
            daemon=True,
        )
        self.reader.start()

    def _read_output(self):
        for line in self.process.stdout:
            self.output.put(line)
        self.output.put(None)

    def read_message(self, context):
        try:
            line = self.output.get(timeout=TIMEOUT_SECONDS)
        except queue.Empty:
            raise AssertionError(
                "Timed out waiting for {} (possible deadlock); "
                "process status: {}".format(context, self.process.poll())
            )
        if line is None:
            stderr = self.process.stderr.read()
            raise AssertionError(
                "Child exited while waiting for {} (status {}): {}".format(
                    context, self.process.poll(), stderr
                )
            )
        return json.loads(line)

    def send(self, request):
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()

    def request(self, request, context):
        self.send(request)
        return self.read_success(context)

    def read_success(self, context):
        response = self.read_message(context)
        if response.get("ok") is not True:
            raise AssertionError(
                "{} failed: {}".format(context, response)
            )
        return response

    def force_kill(self):
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise AssertionError("Could not force-kill child process")

    def close_pipe(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        self.reader.join(timeout=TIMEOUT_SECONDS)
        if self.process.stdout and not self.process.stdout.closed:
            self.process.stdout.close()
        if self.process.stderr and not self.process.stderr.closed:
            self.process.stderr.close()


class CrossProcessConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = os.path.join(
            self.temporary_directory.name, "shared.kv"
        )
        self.workers = []
        self.addCleanup(self.stop_workers)

    def start_worker(self, name):
        worker = DatabaseWorker(self.database_path)
        self.workers.append((name, worker))
        ready = worker.read_message("{} startup".format(name))
        self.assertEqual(ready, {"ready": True})
        return worker

    def stop_workers(self):
        for _, worker in self.workers:
            worker.force_kill()
            worker.close_pipe()

    def verify_after_restart(self, expected, operation_sequence):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = (
            PROJECT_ROOT
            if not environment.get("PYTHONPATH")
            else PROJECT_ROOT + os.pathsep + environment["PYTHONPATH"]
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    VERIFY_SCRIPT,
                    self.database_path,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            self.fail(
                "Restart verification timed out (possible deadlock). "
                "Operation sequence: {}".format(operation_sequence)
            )

        self.assertEqual(
            result.returncode,
            0,
            "Restart verifier failed. Sequence: {}. stdout={!r} stderr={!r}".format(
                operation_sequence, result.stdout, result.stderr
            ),
        )
        self.assertEqual(
            json.loads(result.stdout),
            expected,
            "Fresh process observed lost or inconsistent data. "
            "Operation sequence: {}".format(operation_sequence),
        )

    def test_open_handles_see_sequential_commits_after_forced_kill(self):
        process_a = self.start_worker("Process A")
        process_b = self.start_worker("Process B")
        sequence = [
            "A opens shared database",
            "B opens shared database",
            "A PUT key-A=value-A",
            "B PUT key-B=value-B",
        ]

        process_a.request(
            {"op": "put", "key": "key-A", "value": "value-A"},
            "Process A PUT key-A",
        )
        process_b.request(
            {"op": "put", "key": "key-B", "value": "value-B"},
            "Process B PUT key-B",
        )

        for name, worker in (("A", process_a), ("B", process_b)):
            for key, expected_value in (
                ("key-A", "value-A"),
                ("key-B", "value-B"),
            ):
                sequence.append("{} GET {}".format(name, key))
                response = worker.request(
                    {"op": "get", "key": key},
                    "Process {} GET {}".format(name, key),
                )
                self.assertEqual(
                    response["value"],
                    expected_value,
                    "Stale read or lost write at Process {} GET {}. "
                    "Sequence: {}".format(name, key, sequence),
                )

        self.assertIsNone(process_a.process.poll(), "Process A exited before force-kill")
        self.assertIsNone(process_b.process.poll(), "Process B exited before force-kill")
        process_a.force_kill()
        process_b.force_kill()
        self.verify_after_restart(
            {
                "key-A": "value-A",
                "key-B": "value-B",
                "same-key": None,
            },
            sequence + ["force-kill A and B", "fresh-process GET all"],
        )

    def test_barrier_concurrent_writes_and_same_key_serialized_order(self):
        process_a = self.start_worker("Process A")
        process_b = self.start_worker("Process B")
        sequence = [
            "A and B open shared database",
            "A and B enter write barrier",
        ]

        process_a.send({
            "op": "barrier_put",
            "id": "A",
            "key": "key-A",
            "value": "value-A",
        })
        process_b.send({
            "op": "barrier_put",
            "id": "B",
            "key": "key-B",
            "value": "value-B",
        })
        self.assertEqual(
            process_a.read_message("A reaching write barrier"),
            {"waiting": "A"},
        )
        self.assertEqual(
            process_b.read_message("B reaching write barrier"),
            {"waiting": "B"},
        )

        release_threads = [
            threading.Thread(target=process_a.process.stdin.write, args=("GO\n",)),
            threading.Thread(target=process_b.process.stdin.write, args=("GO\n",)),
        ]
        for thread in release_threads:
            thread.start()
        for thread in release_threads:
            thread.join(timeout=TIMEOUT_SECONDS)
            self.assertFalse(thread.is_alive(), "Barrier release thread timed out")
        process_a.process.stdin.flush()
        process_b.process.stdin.flush()

        process_a.read_success("Process A concurrent PUT")
        process_b.read_success("Process B concurrent PUT")

        for name, worker in (("A", process_a), ("B", process_b)):
            for key, expected_value in (
                ("key-A", "value-A"),
                ("key-B", "value-B"),
            ):
                sequence.append("{} GET {}".format(name, key))
                response = worker.request(
                    {"op": "get", "key": key},
                    "Process {} GET {}".format(name, key),
                )
                self.assertEqual(
                    response["value"],
                    expected_value,
                    "Concurrent update was not visible at Process {} GET {}. "
                    "Sequence: {}".format(name, key, sequence),
                )

        same_key_operations = []
        for name, worker, value in (
            ("A", process_a, "from-A"),
            ("B", process_b, "from-B"),
        ):
            request_id = "same-{}".format(name)
            worker.send({
                "op": "barrier_put",
                "id": request_id,
                "key": "same-key",
                "value": value,
            })
            same_key_operations.append((name, worker, request_id, value))
            sequence.append("{} enters same-key barrier PUT".format(name))

        for name, worker, request_id, _ in same_key_operations:
            self.assertEqual(
                worker.read_message("{} same-key barrier".format(name)),
                {"waiting": request_id},
            )

        release_threads = [
            threading.Thread(
                target=worker.process.stdin.write,
                args=("GO\n",),
            )
            for _, worker, _, _ in same_key_operations
        ]
        for thread in release_threads:
            thread.start()
        for thread in release_threads:
            thread.join(timeout=TIMEOUT_SECONDS)
            self.assertFalse(thread.is_alive(), "Same-key barrier release timed out")
        for _, worker, _, _ in same_key_operations:
            worker.process.stdin.flush()

        for name, worker, _, _ in same_key_operations:
            worker.read_success("Process {} same-key PUT".format(name))

        final_values = []
        for name, worker in (("A", process_a), ("B", process_b)):
            response = worker.request(
                {"op": "get", "key": "same-key"},
                "Process {} GET same-key".format(name),
            )
            final_values.append(response["value"])
            sequence.append("{} GET same-key".format(name))
        self.assertEqual(
            final_values[0],
            final_values[1],
            "Open handles disagree about same-key serialized state. "
            "Sequence: {}".format(sequence),
        )
        self.assertIn(
            final_values[0],
            ("from-A", "from-B"),
            "Same-key result is not from either serialized write. "
            "Sequence: {}".format(sequence),
        )

        self.assertIsNone(process_a.process.poll(), "Process A exited before force-kill")
        self.assertIsNone(process_b.process.poll(), "Process B exited before force-kill")
        process_a.force_kill()
        process_b.force_kill()
        self.verify_after_restart(
            {
                "key-A": "value-A",
                "key-B": "value-B",
                "same-key": final_values[0],
            },
            sequence + ["force-kill A and B", "fresh-process GET all"],
        )


if __name__ == "__main__":
    unittest.main()
