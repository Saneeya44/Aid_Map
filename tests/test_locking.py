
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.locking import FileLock

with FileLock("my_database.kv.lock"):
    print("I have exclusive access to the database!")
    # Database operations go here.

print("The lock has been released.")
