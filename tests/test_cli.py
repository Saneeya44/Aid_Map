import contextlib
import io
import sys
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

    def __init__(self, path):
        self.path = path
        FakeDatabase.instances.append(self)

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


class CliTests(unittest.TestCase):
    def setUp(self):
        FakeDatabase.instances = []
        FakeDatabase.values = {}
        FakeDatabase.put_error = None
        FakeDatabase.delete_error = None
        FakeDatabase.deleted_keys = []
        database_module = types.ModuleType("src.database")
        database_module.Database = FakeDatabase
        self.module_patch = patch.dict(sys.modules, {"src.database": database_module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

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


if __name__ == "__main__":
    unittest.main()
