import errno
import io
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from src import storage
from src.cli import main, serve
from src.database import Database


class DatabaseMaxBytesTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = os.path.join(
            self.temporary_directory.name, "limited.kv"
        )

    def open_database(self, max_bytes=None):
        database = Database(self.database_path, max_bytes=max_bytes)
        self.addCleanup(database.close)
        return database

    def test_put_within_limit_succeeds(self):
        database = self.open_database(max_bytes=100)

        database.put(b"k", b"value")

        self.assertEqual(database.get(b"k"), b"value")
        self.assertLessEqual(os.path.getsize(self.database_path), 100)

    def test_put_exceeding_limit_fails_without_losing_existing_data(self):
        database = self.open_database(max_bytes=30)
        database.put(b"k", b"v")
        previous_size = os.path.getsize(self.database_path)

        with self.assertRaisesRegex(ValueError, "size limit exceeded"):
            database.put(b"another", b"value")

        self.assertEqual(os.path.getsize(self.database_path), previous_size)
        self.assertEqual(database.get(b"k"), b"v")

    def test_exact_size_boundary_succeeds(self):
        record_size = len(storage.encode_record(1, b"\x00\x01kv"))
        database = self.open_database(max_bytes=record_size)

        database.put(b"k", b"v")

        self.assertEqual(os.path.getsize(self.database_path), record_size)

    def test_delete_exceeding_limit_fails_safely(self):
        database = self.open_database(max_bytes=33)
        database.put(b"k", b"v")
        previous_size = os.path.getsize(self.database_path)

        with self.assertRaisesRegex(ValueError, "size limit exceeded"):
            database.delete(b"k")

        self.assertEqual(database.get(b"k"), b"v")
        self.assertEqual(os.path.getsize(self.database_path), previous_size)

    def test_batch_exceeding_limit_is_rejected_atomically(self):
        database = self.open_database(max_bytes=50)
        database.put(b"existing", b"value")
        previous_size = os.path.getsize(self.database_path)

        with self.assertRaisesRegex(ValueError, "size limit exceeded"):
            database.batch([("put", b"a", b"one"), ("delete", b"existing")])

        self.assertEqual(os.path.getsize(self.database_path), previous_size)
        self.assertEqual(database.get(b"existing"), b"value")
        self.assertIsNone(database.get(b"a"))

    def test_enospc_before_writing_preserves_acknowledged_records(self):
        database = self.open_database()
        database.put(b"kept", b"value")

        with mock.patch.object(
            storage.os,
            "write",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            with self.assertRaises(OSError) as raised:
                database.put(b"new", b"value")

        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertEqual(database.get(b"kept"), b"value")
        self.assertIsNone(database.get(b"new"))
        reopened = Database(self.database_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(b"kept"), b"value")

    def test_serve_reports_enospc_without_acknowledging(self):
        request = (
            '{"id":"disk-full","op":"put","key":"aw==","value":"dg=="}\n'
        )
        output = io.StringIO()
        write_error = OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(storage.os, "write", side_effect=write_error):
            result = serve(
                self.database_path,
                input_stream=io.StringIO(request),
                output_stream=output,
                error_stream=io.StringIO(),
            )

        self.assertEqual(result, 0)
        self.assertTrue(output.getvalue().startswith("ERR disk-full "))
        self.assertNotIn("ACK disk-full", output.getvalue())

    def test_enospc_after_partial_write_rolls_back(self):
        database = self.open_database()
        database.put(b"kept", b"value")
        previous_size = os.path.getsize(self.database_path)
        real_write = os.write
        calls = 0

        def partial_then_enospc(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:7])
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(storage.os, "write", side_effect=partial_then_enospc):
            with self.assertRaises(OSError) as raised:
                database.put(b"new", b"value")

        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertEqual(os.path.getsize(self.database_path), previous_size)
        self.assertEqual(database.get(b"kept"), b"value")
        self.assertIsNone(database.get(b"new"))

        reopened = Database(self.database_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(b"kept"), b"value")
        self.assertIsNone(reopened.get(b"new"))

    def test_rollback_failure_reports_original_append_error(self):
        database = self.open_database()
        database.put(b"kept", b"value")
        append_error = OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(storage.os, "write", side_effect=append_error):
            with mock.patch.object(
                storage.os,
                "ftruncate",
                side_effect=OSError(errno.EIO, "rollback blocked"),
            ):
                with self.assertRaisesRegex(OSError, "rollback failed") as raised:
                    database.put(b"new", b"value")

        self.assertIs(raised.exception.__cause__, append_error)

    def test_failed_batch_enospc_is_absent_after_reopen(self):
        database = self.open_database()
        database.put(b"kept", b"value")
        previous_size = os.path.getsize(self.database_path)
        real_write = os.write
        calls = 0

        def partial_then_enospc(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:9])
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(storage.os, "write", side_effect=partial_then_enospc):
            with self.assertRaises(OSError):
                database.batch([("put", b"new", b"value"), ("delete", b"kept")])

        self.assertEqual(os.path.getsize(self.database_path), previous_size)
        reopened = Database(self.database_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(b"kept"), b"value")
        self.assertIsNone(reopened.get(b"new"))

    def test_concurrent_writers_cannot_exceed_limit(self):
        probe_path = os.path.join(self.temporary_directory.name, "probe.kv")
        probe = Database(probe_path)
        probe.put(b"k", b"v")
        one_record_size = os.path.getsize(probe_path)
        probe.close()
        limit = one_record_size

        databases = [Database(self.database_path, max_bytes=limit) for _ in range(2)]
        barrier = threading.Barrier(2)

        def write(database, key):
            barrier.wait()
            try:
                database.put(key, b"v")
                return "written"
            except ValueError as error:
                if "size limit exceeded" not in str(error):
                    raise
                return "rejected"

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda item: write(*item),
                    zip(databases, (b"a", b"b")),
                ))
        finally:
            for database in databases:
                database.close()

        self.assertCountEqual(results, ["written", "rejected"])
        self.assertEqual(os.path.getsize(self.database_path), limit)
        recovered = Database(self.database_path)
        self.addCleanup(recovered.close)
        self.assertEqual(len(recovered.items()), 1)

    def test_open_cli_accepts_max_bytes(self):
        output = io.StringIO()

        with mock.patch("sys.stdout", output):
            result = main([
                "open",
                self.database_path,
                "--max-bytes",
                "10",
            ])

        self.assertEqual(result, 0)
        self.assertIn("Live keys: 0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
