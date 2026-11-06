
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database

DB_FILE = "thread_test.kv"

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database.open(DB_FILE)


def worker(thread_id):

    for i in range(100):

        key = f"thread_{thread_id}_key_{i}".encode()
        value = f"value_{thread_id}_{i}".encode()

        db.put(key, value)

        result = db.get(key)

        assert result == value


# Run 10 threads simultaneously
with ThreadPoolExecutor(max_workers=10) as executor:

    executor.map(worker, range(10))


print("All threads completed successfully!")

# Verify all 1000 records
for thread_id in range(10):

    for i in range(100):

        key = f"thread_{thread_id}_key_{i}".encode()
        expected = f"value_{thread_id}_{i}".encode()

        assert db.get(key) == expected


print("All 1000 records verified!")

# Test persistence after restart
db.close()

db2 = Database.open(DB_FILE)

assert db2.get(b"thread_5_key_50") == b"value_5_50"

print("Recovery test passed!")

db2.close()

print("THREAD SAFETY TEST PASSED!")
