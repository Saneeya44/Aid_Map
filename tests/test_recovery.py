
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database

DB_FILE = "test_recovery.kv"

# Remove the old test file, if it exists
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

# Step 1: Store valid data
db = Database(DB_FILE)
db.put(b"name", b"Anu")
db.put(b"age", b"20")

print("Before corruption:")
print(db.get(b"name"))
print(db.get(b"age"))

print("\nFile size before recovery:", os.path.getsize(DB_FILE))

# Step 2: Add incomplete bytes to simulate a crash
with open(DB_FILE, "ab") as file:
    file.write(b"KV")

print("\nIncomplete bytes added!")

# Step 3: Reopen the database
db2 = Database(DB_FILE)

print("\nAfter recovery:")
print(db2.get(b"name"))
print(db2.get(b"age"))

print("\nFile size after recovery:", os.path.getsize(DB_FILE))
