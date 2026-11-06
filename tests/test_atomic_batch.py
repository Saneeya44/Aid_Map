
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database

DB_FILE = "test_atomic.kv"

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

# Create database
db = Database(DB_FILE)

# Store initial values
db.put(b"name", b"Anu")
db.put(b"age", b"20")

# Remember where the batch begins
batch_start = os.path.getsize(DB_FILE)

# Write a batch
db.batch([
    ("put", b"name", b"Ananya"),
    ("put", b"city", b"Mysuru"),
    ("delete", b"age")
])

# Read complete database file
with open(DB_FILE, "rb") as file:
    full_data = file.read()

# Simulate a crash during the batch payload
with open(DB_FILE, "wb") as file:
    file.write(full_data[:batch_start + 16])

# Restart database
db2 = Database(DB_FILE)

print("After simulated crash:")

print("Name:", db2.get(b"name"))
print("Age:", db2.get(b"age"))
print("City:", db2.get(b"city"))

# Verify atomicity
assert db2.get(b"name") == b"Anu"
assert db2.get(b"age") == b"20"
assert db2.get(b"city") is None

print("\nATOMICITY TEST PASSED!")
