
import os
import sys


class FileLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = open(self.path, "a+b")

        # Windows needs at least one byte to lock.
        self.file.seek(0, os.SEEK_END)

        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()

        self.file.seek(0)

        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(
                self.file.fileno(),
                msvcrt.LK_LOCK,
                1
            )
        else:
            import fcntl
            fcntl.flock(
                self.file.fileno(),
                fcntl.LOCK_EX
            )

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.file.seek(0)

        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(
                self.file.fileno(),
                msvcrt.LK_UNLCK,
                1
            )
        else:
            import fcntl
            fcntl.flock(
                self.file.fileno(),
                fcntl.LOCK_UN
            )

        self.file.close()
