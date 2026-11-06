import os
import tempfile
import unittest

from src.database import Database, PUT
from src.storage import encode_record


class StrictDatabaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = os.path.join(
            self.temporary_directory.name, "strict-check.kv"
        )
        self.database = Database(self.database_path)
        self.addCleanup(self.database.close)

    def read_database_bytes(self):
        with open(self.database_path, "rb") as database_file:
            return database_file.read()

    def read_lock_bytes(self):
        with open(self.database_path + ".lock", "rb") as lock_file:
            return lock_file.read()

    def test_valid_check_succeeds_without_changing_database_bytes(self):
        self.database.put(b"key", b"value")
        original_bytes = self.read_database_bytes()
        original_lock_bytes = self.read_lock_bytes()

        self.assertIs(Database.check_file(self.database_path), True)
        self.assertEqual(self.read_database_bytes(), original_bytes)
        self.assertEqual(self.read_lock_bytes(), original_lock_bytes)

    def test_check_of_missing_database_does_not_create_files(self):
        missing_path = os.path.join(
            self.temporary_directory.name, "missing.kv"
        )

        with self.assertRaises(FileNotFoundError):
            Database.check_file(missing_path)

        self.assertFalse(os.path.exists(missing_path))
        self.assertFalse(os.path.exists(missing_path + ".lock"))

    def test_incomplete_trailing_header_is_reported_without_repair(self):
        self.database.put(b"key", b"value")
        with open(self.database_path, "ab") as database_file:
            database_file.write(b"KV")
        original_bytes = self.read_database_bytes()
        original_size = len(original_bytes)

        with self.assertRaisesRegex(ValueError, "Incomplete trailing record header"):
            Database.check_file(self.database_path)

        self.assertEqual(self.read_database_bytes(), original_bytes)
        self.assertEqual(os.path.getsize(self.database_path), original_size)

    def test_incomplete_trailing_payload_is_reported_without_repair(self):
        partial_record = encode_record(PUT, b"\x00\x03keyvalue")[:-2]
        with open(self.database_path, "ab") as database_file:
            database_file.write(partial_record)
        original_bytes = self.read_database_bytes()
        original_size = len(original_bytes)

        with self.assertRaisesRegex(ValueError, "Incomplete trailing record payload"):
            Database.check_file(self.database_path)

        self.assertEqual(self.read_database_bytes(), original_bytes)
        self.assertEqual(os.path.getsize(self.database_path), original_size)

    def test_corrupted_magic_version_and_checksum_are_rejected(self):
        record = encode_record(PUT, b"\x00\x03keyvalue")
        corruption_offsets = {
            "magic": 0,
            "version": 4,
            "checksum": 10,
        }

        for corruption, offset in corruption_offsets.items():
            with self.subTest(corruption=corruption):
                with open(self.database_path, "wb") as database_file:
                    database_file.write(record)
                original_bytes = self.read_database_bytes()

                with open(self.database_path, "r+b") as database_file:
                    database_file.seek(offset)
                    byte = database_file.read(1)
                    database_file.seek(offset)
                    database_file.write(bytes([byte[0] ^ 0x01]))
                corrupted_bytes = self.read_database_bytes()

                with self.assertRaises(ValueError):
                    Database.check_file(self.database_path)

                self.assertEqual(self.read_database_bytes(), corrupted_bytes)
                self.assertNotEqual(corrupted_bytes, original_bytes)

    def test_normal_recovery_still_repairs_incomplete_tail(self):
        self.database.put(b"key", b"value")
        valid_bytes = self.read_database_bytes()
        with open(self.database_path, "ab") as database_file:
            database_file.write(b"KV")
        damaged_size = os.path.getsize(self.database_path)
        self.database.close()

        recovered_database = Database(self.database_path)
        self.addCleanup(recovered_database.close)

        self.assertGreater(damaged_size, len(valid_bytes))
        self.assertEqual(self.read_database_bytes(), valid_bytes)
        self.assertEqual(recovered_database.get(b"key"), b"value")

    def test_noncreating_lock_mode_requires_an_existing_sidecar(self):
        self.database.close()
        os.remove(self.database_path + ".lock")
        original_bytes = self.read_database_bytes()

        with self.assertRaisesRegex(RuntimeError, "existing lock file"):
            Database.check_file(self.database_path)

        self.assertEqual(self.read_database_bytes(), original_bytes)
        self.assertFalse(os.path.exists(self.database_path + ".lock"))


if __name__ == "__main__":
    unittest.main()
