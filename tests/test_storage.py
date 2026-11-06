import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage import append_record, read_records

DB_FILE = "test.kv"

append_record(DB_FILE, 1, b"name=Anu")
append_record(DB_FILE, 1, b"age=20")

records = read_records(DB_FILE)

print(records)

os.remove(DB_FILE)
