import os
import tempfile
import unittest
from unittest.mock import patch

from src.database import Database


class DatabaseReadOnlyApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = os.path.join(
            self.temporary_directory.name, "test.kv"
        )
        self.database = Database(self.database_path)
        self.addCleanup(self.database.close)

    def test_key_count_counts_only_live_keys(self):
        self.database.put(b"name", b"Anu")
        self.database.put(b"age", b"20")
        self.database.put(b"name", b"Ananya")

        self.assertEqual(self.database.key_count(), 2)

        self.database.delete(b"age")
        self.assertEqual(self.database.key_count(), 1)

    def test_items_returns_a_snapshot_of_binary_byte_pairs(self):
        expected = {
            b"\x00key": b"\xff\x00value",
            b"name": b"Anu",
        }
        for key, value in expected.items():
            self.database.put(key, value)

        entries = self.database.items()

        self.assertIsInstance(entries, list)
        self.assertEqual(dict(entries), expected)
        self.assertTrue(
            all(
                isinstance(key, bytes) and isinstance(value, bytes)
                for key, value in entries
            )
        )

        self.database.delete(b"name")
        self.assertEqual(dict(entries), expected)
        self.assertEqual(dict(self.database.items()), {b"\x00key": b"\xff\x00value"})

    def test_compaction_reclaims_obsolete_values_and_tombstones(self):
        self.database.put(b"updated", b"old value" * 100)
        self.database.put(b"deleted", b"deleted value" * 100)
        self.database.put(b"updated", b"current")
        self.database.delete(b"deleted")
        size_before = os.path.getsize(self.database_path)

        reported_before, reported_after = self.database.compact()

        self.assertEqual(reported_before, size_before)
        self.assertEqual(reported_after, os.path.getsize(self.database_path))
        self.assertLess(os.path.getsize(self.database_path), size_before)
        self.assertEqual(dict(self.database.items()), {b"updated": b"current"})
        self.assertIs(self.database.check(), True)
        with Database(self.database_path) as reopened:
            self.assertEqual(dict(reopened.items()), {b"updated": b"current"})

    def test_compaction_empty_database_replaces_log_with_empty_snapshot(self):
        self.database.put(b"temporary", b"value")
        self.database.delete(b"temporary")

        self.database.compact()

        self.assertEqual(os.path.getsize(self.database_path), 0)
        self.assertEqual(self.database.key_count(), 0)

    def test_failed_compaction_preserves_original_database(self):
        self.database.put(b"key", b"value")
        with open(self.database_path, "rb") as file:
            original = file.read()

        with patch(
            "src.storage.os.replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.database.compact()

        with open(self.database_path, "rb") as file:
            self.assertEqual(file.read(), original)
        self.assertEqual(self.database.get(b"key"), b"value")

    def test_compaction_fsync_failure_preserves_original_database(self):
        self.database.put(b"key", b"value")
        with open(self.database_path, "rb") as file:
            original = file.read()
        directory_contents = set(os.listdir(self.temporary_directory.name))

        with patch(
            "src.storage.os.fsync", side_effect=OSError("simulated disk full")
        ):
            with self.assertRaisesRegex(OSError, "simulated disk full"):
                self.database.compact()

        with open(self.database_path, "rb") as file:
            self.assertEqual(file.read(), original)
        self.assertEqual(
            set(os.listdir(self.temporary_directory.name)),
            directory_contents,
        )
        self.assertEqual(self.database.get(b"key"), b"value")

    def test_read_only_methods_refresh_from_another_database_instance(self):
        other_database = Database(
            os.path.join(self.temporary_directory.name, "test.kv")
        )
        self.addCleanup(other_database.close)
        other_database.put(b"shared", b"value")

        self.assertEqual(self.database.key_count(), 1)
        self.assertEqual(self.database.items(), [(b"shared", b"value")])

    def test_check_returns_true_for_valid_database(self):
        self.database.put(b"name", b"Anu")

        self.assertIs(self.database.check(), True)
        self.assertIs(Database.check_file(self.database_path), True)

    def test_check_raises_for_checksum_corruption(self):
        self.database.put(b"name", b"Anu")

        with open(self.database_path, "r+b") as database_file:
            database_file.seek(-1, os.SEEK_END)
            last_byte = database_file.read(1)
            database_file.seek(-1, os.SEEK_END)
            database_file.write(bytes([last_byte[0] ^ 0x01]))

        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            Database.check_file(self.database_path)

    def test_read_only_methods_reject_closed_database(self):
        self.database.close()

        for method in (
            self.database.key_count,
            self.database.items,
            self.database.check,
        ):
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(RuntimeError, "Database is closed"):
                    method()


if __name__ == "__main__":
    unittest.main()
