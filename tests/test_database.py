import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database

db = Database("database.kv")

# Store data
db.put(b"name", b"Anu")
db.put(b"age", b"20")

# Retrieve data
print(db.get(b"name"))
print(db.get(b"age"))

# Update existing data
db.put(b"name", b"Ananya")

print(db.get(b"name"))
