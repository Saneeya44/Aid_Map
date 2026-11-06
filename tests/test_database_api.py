import os
import tempfile
import unittest

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

    def test_check_raises_for_checksum_corruption(self):
        self.database.put(b"name", b"Anu")

        with open(self.database_path, "r+b") as database_file:
            database_file.seek(-1, os.SEEK_END)
            last_byte = database_file.read(1)
            database_file.seek(-1, os.SEEK_END)
            database_file.write(bytes([last_byte[0] ^ 0x01]))

        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            self.database.check()

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
