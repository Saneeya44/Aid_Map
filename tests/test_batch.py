
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database

DB_FILE = "test_batch.kv"

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE)

# Add initial data
db.put(b"name", b"Anu")
db.put(b"age", b"20")

print("Before batch:")
print(db.get(b"name"))
print(db.get(b"age"))

# Execute batch
db.batch([
    ("put", b"name", b"Ananya"),
    ("put", b"city", b"Mysuru"),
    ("delete", b"age")
])

print("\nAfter batch:")
print(db.get(b"name"))
print(db.get(b"age"))
print(db.get(b"city"))

# Restart database
db2 = Database(DB_FILE)

print("\nAfter restarting:")
print(db2.get(b"name"))
print(db2.get(b"age"))
print(db2.get(b"city"))
