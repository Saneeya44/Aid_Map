import contextlib
import base64
import io
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from src import cli


class FakeDatabase:
    instances = []
    values = {}
    put_error = None
    delete_error = None
    deleted_keys = []
    batch_error = None
    batch_calls = []
    check_error = None
    check_result = True

    def __init__(self, path):
        self.path = path
        FakeDatabase.instances.append(self)

    def key_count(self):
        return len(FakeDatabase.values)

    def items(self):
        return list(FakeDatabase.values.items())

    def check(self):
        if FakeDatabase.check_error is not None:
            raise FakeDatabase.check_error
        return FakeDatabase.check_result

    def put(self, key, value):
        if FakeDatabase.put_error is not None:
            raise FakeDatabase.put_error
        FakeDatabase.values[key] = value

    def get(self, key):
        return FakeDatabase.values.get(key)

    def delete(self, key):
        if FakeDatabase.delete_error is not None:
            raise FakeDatabase.delete_error
        FakeDatabase.deleted_keys.append(key)
        FakeDatabase.values.pop(key, None)

    def batch(self, operations):
        FakeDatabase.batch_calls.append(operations)
        if FakeDatabase.batch_error is not None:
            raise FakeDatabase.batch_error


class CliTests(unittest.TestCase):
    def setUp(self):
        FakeDatabase.instances = []
        FakeDatabase.values = {}
        FakeDatabase.put_error = None
        FakeDatabase.delete_error = None
        FakeDatabase.deleted_keys = []
        FakeDatabase.batch_error = None
        FakeDatabase.batch_calls = []
        FakeDatabase.check_error = None
        FakeDatabase.check_result = True
        database_module = types.ModuleType("src.database")
        database_module.Database = FakeDatabase
        self.module_patch = patch.dict(sys.modules, {"src.database": database_module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def write_batch_file(self, contents):
        file_descriptor, path = tempfile.mkstemp()
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            file.write(contents)
        self.addCleanup(os.remove, path)
        return path

    def run_cli(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = cli.main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_put_encodes_key_and_value_and_prints_ok(self):
        exit_code, stdout, stderr = self.run_cli(
            ["put", "test.kv", "name", "Rahul"]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "OK\n")
        self.assertEqual(stderr, "")
        database = FakeDatabase.instances[0]
        self.assertEqual(database.path, "test.kv")
        self.assertEqual(FakeDatabase.values, {b"name": b"Rahul"})

    def test_get_prints_value(self):
        database = FakeDatabase("test.kv")
        FakeDatabase.values[b"name"] = "Rahul".encode("utf-8")

        exit_code, stdout, stderr = self.run_cli(
            ["get", "test.kv", "name"]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "Rahul\n")
        self.assertEqual(stderr, "")

    def test_get_prints_notfound_for_missing_key(self):
        exit_code, stdout, stderr = self.run_cli(
            ["get", "test.kv", "missing"]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "NOTFOUND\n")
        self.assertEqual(stderr, "")

    def test_put_error_does_not_print_success(self):
        FakeDatabase.put_error = ValueError("invalid key")

        exit_code, stdout, stderr = self.run_cli(
            ["put", "test.kv", "", "Rahul"]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid key", stderr)

    def test_delete_encodes_key_and_prints_ok(self):
        exit_code, stdout, stderr = self.run_cli(
            ["del", "test.kv", "café"]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "OK\n")
        self.assertEqual(stderr, "")
        self.assertEqual(FakeDatabase.instances[0].path, "test.kv")
        self.assertEqual(FakeDatabase.deleted_keys, ["café".encode("utf-8")])

    def test_delete_error_does_not_print_success(self):
        FakeDatabase.delete_error = ValueError("delete failed")

        exit_code, stdout, stderr = self.run_cli(
            ["del", "test.kv", "name"]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("delete failed", stderr)

    def test_arguments_are_utf8_encoded(self):
        exit_code, stdout, stderr = self.run_cli(
            ["put", "test.kv", "café", "こんにちは"]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "OK\n")
        self.assertEqual(stderr, "")
        self.assertEqual(FakeDatabase.values, {
            "café".encode("utf-8"): "こんにちは".encode("utf-8")
        })

    def test_batch_calls_database_once_with_encoded_operations(self):
        batch_file = self.write_batch_file(
            "PUT name Rahul\nPUT age 20\nDEL old\n"
        )

        exit_code, stdout, stderr = self.run_cli(
            ["batch", "test.kv", batch_file]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "OK\n")
        self.assertEqual(stderr, "")
        self.assertEqual(len(FakeDatabase.batch_calls), 1)
        self.assertEqual(
            FakeDatabase.batch_calls[0],
            [
                ("put", b"name", b"Rahul"),
                ("put", b"age", b"20"),
                ("delete", b"old", None),
            ],
        )

    def test_batch_preserves_spaces_in_put_value(self):
        batch_file = self.write_batch_file("PUT greeting hello from the CLI\n")

        exit_code, stdout, stderr = self.run_cli(
            ["batch", "test.kv", batch_file]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "OK\n")
        self.assertEqual(stderr, "")
        self.assertEqual(
            FakeDatabase.batch_calls,
            [[("put", b"greeting", b"hello from the CLI")]],
        )

    def test_batch_rejects_invalid_operation_without_calling_database(self):
        batch_file = self.write_batch_file("SET name Rahul\n")

        exit_code, stdout, stderr = self.run_cli(
            ["batch", "test.kv", batch_file]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Invalid operation", stderr)
        self.assertEqual(FakeDatabase.batch_calls, [])

    def test_malformed_batch_does_not_call_database(self):
        batch_file = self.write_batch_file("PUT name\nDEL old extra\n")

        exit_code, stdout, stderr = self.run_cli(
            ["batch", "test.kv", batch_file]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Malformed PUT", stderr)
        self.assertEqual(FakeDatabase.batch_calls, [])

    def test_missing_batch_file_returns_error(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_path = os.path.join(directory, "missing.txt")

            exit_code, stdout, stderr = self.run_cli(
                ["batch", "test.kv", missing_path]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Error:", stderr)
        self.assertEqual(FakeDatabase.batch_calls, [])

    def test_batch_database_failure_does_not_print_success(self):
        FakeDatabase.batch_error = ValueError("batch failed")
        batch_file = self.write_batch_file("PUT name Rahul\n")

        exit_code, stdout, stderr = self.run_cli(
            ["batch", "test.kv", batch_file]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("batch failed", stderr)
        self.assertEqual(len(FakeDatabase.batch_calls), 1)

    def test_batch_rejects_more_than_10000_operations(self):
        batch_file = self.write_batch_file("DEL key\n" * 10001)

        exit_code, stdout, stderr = self.run_cli(
            ["batch", "test.kv", batch_file]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("cannot exceed 10000", stderr)
        self.assertEqual(FakeDatabase.batch_calls, [])

    def test_batch_reports_missing_database_batch_method(self):
        batch_file = self.write_batch_file("PUT name Rahul\n")

        with patch.object(FakeDatabase, "batch", None):
            exit_code, stdout, stderr = self.run_cli(
                ["batch", "test.kv", batch_file]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Database.batch() is not available", stderr)
        self.assertEqual(FakeDatabase.batch_calls, [])

    def test_open_reports_live_key_count_and_elapsed_time(self):
        FakeDatabase.values = {b"one": b"1", b"two": b"2"}

        exit_code, stdout, stderr = self.run_cli(["open", "test.kv"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Live keys: 2\n", stdout)
        self.assertIn("Open/recovery time (includes initialization): ", stdout)
        elapsed_text = stdout.split(": ")[-1].removesuffix(" ms\n")
        self.assertGreaterEqual(float(elapsed_text), 0)

    def test_dump_sorts_binary_keys_and_values_as_base64(self):
        FakeDatabase.values = {
            b"\xff": b"\x00\xfe",
            b"\x00": b"\xff\x01",
            b"a": b"text",
        }

        exit_code, stdout, stderr = self.run_cli(["dump", "test.kv"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        expected = [
            "key={} value={}".format(
                base64.b64encode(key).decode("ascii"),
                base64.b64encode(value).decode("ascii"),
            )
            for key, value in sorted(FakeDatabase.values.items())
        ]
        self.assertEqual(stdout, "\n".join(expected) + "\n")

    def test_check_reports_verified_integrity(self):
        exit_code, stdout, stderr = self.run_cli(["check", "test.kv"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "OK: database integrity verified\n")
        self.assertEqual(stderr, "")

    def test_check_reports_corruption_when_engine_returns_false(self):
        FakeDatabase.check_result = False

        exit_code, stdout, stderr = self.run_cli(["check", "test.kv"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("did not confirm integrity", stderr)

    def test_check_reports_engine_failure(self):
        FakeDatabase.check_error = ValueError("checksum mismatch")

        exit_code, stdout, stderr = self.run_cli(["check", "test.kv"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("checksum mismatch", stderr)

    def test_open_dump_and_check_report_missing_engine_methods(self):
        cases = [
            ("open", "key_count", "Database.key_count() is not available"),
            ("dump", "items", "Database.items() is not available"),
            ("check", "check", "Database.check() is not available"),
        ]
        for command, method_name, expected_error in cases:
            with self.subTest(command=command):
                with patch.object(FakeDatabase, method_name, None):
                    exit_code, stdout, stderr = self.run_cli(
                        [command, "test.kv"]
                    )

                self.assertEqual(exit_code, 1)
                self.assertEqual(stdout, "")
                self.assertIn(expected_error, stderr)


if __name__ == "__main__":
    unittest.main()
