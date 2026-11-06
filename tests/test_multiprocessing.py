
import os
import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database

DB_FILE = "multiprocess_test.kv"


def worker(process_id):

    db = Database.open(DB_FILE)

    for i in range(25):

        key = f"process_{process_id}_key_{i}".encode()
        value = f"value_{process_id}_{i}".encode()

        db.put(key, value)

    db.close()


if __name__ == "__main__":

    # Remove previous test files
    for path in (DB_FILE, DB_FILE + ".lock"):
        if os.path.exists(path):
            os.remove(path)

    processes = []

    # Start four independent processes
    for process_id in range(4):

        process = multiprocessing.Process(
            target=worker,
            args=(process_id,)
        )

        processes.append(process)
        process.start()

    # Wait for every process
    for process in processes:
        process.join()

    # Check for process failures
    for process in processes:
        assert process.exitcode == 0, (
            f"Process failed: {process.exitcode}"
        )

    # Reopen database and verify all records
    db = Database.open(DB_FILE)

    count = 0

    for process_id in range(4):

        for i in range(25):

            key = f"process_{process_id}_key_{i}".encode()
            expected = f"value_{process_id}_{i}".encode()

            assert db.get(key) == expected

            count += 1

    print("All processes completed!")
    print("Total records verified:", count)
    print("MULTIPROCESSING TEST PASSED!")

    db.close()
