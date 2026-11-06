
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database

DB_FILE = "test_delete.kv"

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE)

# Store data
db.put(b"name", b"Anu")
db.put(b"age", b"20")

print("Before deletion:")
print(db.get(b"name"))
print(db.get(b"age"))

# Delete name
print("\nDelete result:", db.delete(b"name"))

print("\nAfter deletion:")
print(db.get(b"name"))
print(db.get(b"age"))

# Try deleting the same key again
print("\nDelete again:", db.delete(b"name"))

# Reopen database to test recovery
db2 = Database(DB_FILE)

print("\nAfter restarting:")
print(db2.get(b"name"))
print(db2.get(b"age"))
