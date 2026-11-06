import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.cli import serve
from src.database import Database


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def request_line(request):
    return json.dumps(request) + "\n"


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = os.path.join(
            self.temporary_directory.name, "serve.kv"
        )

    def run_serve_process(self, requests):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli",
                "serve",
                self.database_path,
            ],
            cwd=PROJECT_ROOT,
            input=requests,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_put_ack_is_durable_and_value_survives_restart(self):
        request = request_line({
            "id": "op-1",
            "op": "put",
            "key": encoded(b"name"),
            "value": encoded(b"Ananya"),
        })

        result = self.run_serve_process(request)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ACK op-1\n")
        self.assertEqual(result.stderr, "")
        with Database(self.database_path) as database:
            self.assertEqual(database.get(b"name"), b"Ananya")

    def test_delete_ack_leaves_key_absent_after_restart(self):
        with Database(self.database_path) as database:
            database.put(b"name", b"Ananya")

        request = request_line({
            "id": "op-2",
            "op": "delete",
            "key": encoded(b"name"),
        })
        result = self.run_serve_process(request)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ACK op-2\n")
        self.assertEqual(result.stderr, "")
        with Database(self.database_path) as database:
            self.assertIsNone(database.get(b"name"))

    def test_batch_is_one_call_and_emits_one_ack(self):
        batch_request = request_line({
            "id": "op-3",
            "op": "batch",
            "operations": [
                {"op": "put", "key": encoded(b"a"), "value": encoded(b"1")},
                {"op": "delete", "key": encoded(b"b")},
            ],
        })
        original_batch = Database.batch
        batch_calls = []

        def batch_spy(database, operations):
            batch_calls.append(operations)
            return original_batch(database, operations)

        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(Database, "batch", batch_spy):
            exit_code = serve(
                self.database_path,
                input_stream=io.StringIO(batch_request),
                output_stream=output,
                error_stream=errors,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "ACK op-3\n")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(
            batch_calls[0],
            [("put", b"a", b"1"), ("delete", b"b")],
        )
        with Database(self.database_path) as database:
            self.assertEqual(database.get(b"a"), b"1")

    def test_invalid_batch_is_rejected_without_partial_application(self):
        request = request_line({
            "id": "op-bad",
            "op": "batch",
            "operations": [
                {"op": "put", "key": encoded(b"partial"), "value": encoded(b"x")},
                {"op": "delete", "key": "%%%"},
            ],
        })

        result = self.run_serve_process(request)

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("ERR op-bad "))
        self.assertNotIn("ACK", result.stdout)
        self.assertEqual(result.stderr, "")
        with Database(self.database_path) as database:
            self.assertIsNone(database.get(b"partial"))

    def test_invalid_json_and_base64_do_not_stop_later_requests(self):
        requests = (
            "{not json}\n"
            + request_line({
                "id": "bad-base64",
                "op": "put",
                "key": "%%%not-base64%%%",
                "value": encoded(b"value"),
            })
            + request_line({
                "id": "op-good",
                "op": "put",
                "key": encoded(b"valid"),
                "value": encoded(b"after errors"),
            })
        )

        result = self.run_serve_process(requests)

        self.assertEqual(result.returncode, 0)
        responses = result.stdout.splitlines()
        self.assertEqual(len(responses), 3)
        self.assertTrue(responses[0].startswith("ERR - "))
        self.assertTrue(responses[1].startswith("ERR bad-base64 "))
        self.assertEqual(responses[2], "ACK op-good")
        self.assertEqual(result.stderr, "")
        with Database(self.database_path) as database:
            self.assertEqual(database.get(b"valid"), b"after errors")

    def test_key_value_and_batch_limits_are_validated(self):
        requests = (
            request_line({
                "id": "long-key",
                "op": "put",
                "key": encoded(b"k" * 1025),
                "value": encoded(b"value"),
            })
            + request_line({
                "id": "long-value",
                "op": "put",
                "key": encoded(b"key"),
                "value": encoded(b"v" * (1024 * 1024 + 1)),
            })
            + request_line({
                "id": "empty-batch",
                "op": "batch",
                "operations": [],
            })
            + request_line({
                "id": "large-batch",
                "op": "batch",
                "operations": [
                    {"op": "delete", "key": encoded(b"key")}
                    for _ in range(10001)
                ],
            })
        )

        result = self.run_serve_process(requests)

        self.assertEqual(result.returncode, 0)
        responses = result.stdout.splitlines()
        self.assertEqual(len(responses), 4)
        self.assertTrue(responses[0].startswith("ERR long-key "))
        self.assertTrue(responses[1].startswith("ERR long-value "))
        self.assertTrue(responses[2].startswith("ERR empty-batch "))
        self.assertTrue(responses[3].startswith("ERR large-batch "))
        self.assertEqual(result.stderr, "")
        with Database(self.database_path) as database:
            self.assertEqual(database.key_count(), 0)

    def test_invalid_operation_id_uses_dash_error_id(self):
        request = request_line({
            "id": "bad id",
            "op": "put",
            "key": encoded(b"key"),
            "value": encoded(b"value"),
        })
        output = io.StringIO()

        exit_code = serve(
            self.database_path,
            input_stream=io.StringIO(request),
            output_stream=output,
            error_stream=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(output.getvalue().startswith("ERR - "))

    def test_reserved_dash_operation_id_is_rejected(self):
        request = request_line({
            "id": "-",
            "op": "put",
            "key": encoded(b"key"),
            "value": encoded(b"value"),
        })
        output = io.StringIO()

        exit_code = serve(
            self.database_path,
            input_stream=io.StringIO(request),
            output_stream=output,
            error_stream=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(output.getvalue().startswith("ERR - "))

    def test_multiple_requests_and_eof_exit_cleanly(self):
        requests = "".join(
            request_line({
                "id": "op-{}".format(index),
                "op": "put",
                "key": encoded("key{}".format(index).encode()),
                "value": encoded("value{}".format(index).encode()),
            })
            for index in range(3)
        )

        result = self.run_serve_process(requests)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout, "ACK op-0\nACK op-1\nACK op-2\n"
        )
        self.assertEqual(result.stderr, "")

    def test_response_is_flushed_for_each_request(self):
        class FlushTrackingStream(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flush_count = 0

            def flush(self):
                self.flush_count += 1
                super().flush()

        requests = "".join(
            request_line({
                "id": "flush-{}".format(index),
                "op": "delete",
                "key": encoded(b"missing"),
            })
            for index in range(2)
        )
        output = FlushTrackingStream()

        exit_code = serve(
            self.database_path,
            input_stream=io.StringIO(requests),
            output_stream=output,
            error_stream=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "ACK flush-0\nACK flush-1\n")
        self.assertEqual(output.flush_count, 2)

    def test_protocol_errors_are_single_line_and_stdout_is_protocol_only(self):
        requests = (
            request_line({"id": "op-err", "op": "unknown"})
            + request_line({
                "id": "op-ok",
                "op": "put",
                "key": encoded(b"key"),
                "value": encoded(b"value"),
            })
        )

        result = self.run_serve_process(requests)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()), 2)
        self.assertTrue(result.stdout.startswith("ERR op-err "))
        self.assertEqual(result.stdout.splitlines()[1], "ACK op-ok")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
