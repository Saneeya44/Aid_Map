
import os
import struct
import threading

from .storage import append_record, read_records
from .locking import FileLock


PUT = 1
DELETE = 2
BATCH = 3

MAX_KEY_LENGTH = 1024
MAX_VALUE_LENGTH = 1024 * 1024
MAX_BATCH_OPERATIONS = 10000


class Database:

    def __init__(self, path):
        self.path = os.fspath(path)
        self.lock_path = self.path + ".lock"
        self.index = {}
        self.lock = threading.RLock()
        self.closed = False

        with open(self.path, "ab"):
            pass

        # Recover while holding the process lock
        with self.lock:
            with FileLock(self.lock_path):
                self._recover()

    @classmethod
    def open(cls, path):
        return cls(path)

    def _ensure_open(self):
        if self.closed:
            raise RuntimeError("Database is closed")

    # --------------------------------
    # PUT ENCODING
    # --------------------------------

    def _encode_put(self, key, value):

        if not isinstance(key, bytes):
            raise TypeError("Key must be bytes")

        if not isinstance(value, bytes):
            raise TypeError("Value must be bytes")

        if not 1 <= len(key) <= MAX_KEY_LENGTH:
            raise ValueError("Invalid key length")

        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError("Value too large")

        return struct.pack(">H", len(key)) + key + value

    def _decode_put(self, payload):

        if len(payload) < 2:
            raise ValueError("Invalid PUT payload")

        key_length = struct.unpack(">H", payload[:2])[0]

        if not 1 <= key_length <= MAX_KEY_LENGTH:
            raise ValueError("Invalid key length")

        if len(payload) < 2 + key_length:
            raise ValueError("Incomplete PUT payload")

        key = payload[2:2 + key_length]
        value = payload[2 + key_length:]

        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError("Value too large")

        return key, value

    # --------------------------------
    # DELETE ENCODING
    # --------------------------------

    def _encode_delete(self, key):

        if not isinstance(key, bytes):
            raise TypeError("Key must be bytes")

        if not 1 <= len(key) <= MAX_KEY_LENGTH:
            raise ValueError("Invalid key length")

        return struct.pack(">H", len(key)) + key

    def _decode_delete(self, payload):

        if len(payload) < 2:
            raise ValueError("Invalid DELETE payload")

        key_length = struct.unpack(">H", payload[:2])[0]

        if not 1 <= key_length <= MAX_KEY_LENGTH:
            raise ValueError("Invalid key length")

        if len(payload) != 2 + key_length:
            raise ValueError("Invalid DELETE payload")

        return payload[2:]

    # --------------------------------
    # BATCH ENCODING
    # --------------------------------

    def _encode_batch(self, operations):

        if not isinstance(operations, (list, tuple)):
            raise TypeError("Operations must be a list or tuple")

        if len(operations) > MAX_BATCH_OPERATIONS:
            raise ValueError("Too many batch operations")

        payload = struct.pack(">I", len(operations))

        for operation in operations:

            if not isinstance(operation, (list, tuple)):
                raise TypeError("Invalid operation")

            if len(operation) == 3 and operation[0] == "put":

                _, key, value = operation

                encoded = self._encode_put(key, value)

                payload += struct.pack(">BH", PUT, len(key))
                payload += key
                payload += struct.pack(">I", len(value))
                payload += value

            elif (
                len(operation) == 2
                and operation[0] == "delete"
            ) or (
                len(operation) == 3
                and operation[0] == "delete"
                and operation[2] is None
            ):

                key = operation[1]

                self._encode_delete(key)

                payload += struct.pack(">BH", DELETE, len(key))
                payload += key

            else:
                raise ValueError("Invalid batch operation")

        return payload

    # --------------------------------
    # BATCH DECODING
    # --------------------------------

    def _decode_batch(self, payload):

        if len(payload) < 4:
            raise ValueError("Invalid batch payload")

        count = struct.unpack(">I", payload[:4])[0]

        if count > MAX_BATCH_OPERATIONS:
            raise ValueError("Too many batch operations")

        position = 4
        operations = []

        for _ in range(count):

            if len(payload) - position < 3:
                raise ValueError("Incomplete batch operation")

            op_type, key_length = struct.unpack(
                ">BH", payload[position:position + 3]
            )

            position += 3

            if not 1 <= key_length <= MAX_KEY_LENGTH:
                raise ValueError("Invalid key length")

            if len(payload) - position < key_length:
                raise ValueError("Incomplete batch key")

            key = payload[position:position + key_length]
            position += key_length

            if op_type == PUT:

                if len(payload) - position < 4:
                    raise ValueError("Incomplete value length")

                value_length = struct.unpack(
                    ">I", payload[position:position + 4]
                )[0]

                position += 4

                if value_length > MAX_VALUE_LENGTH:
                    raise ValueError("Value too large")

                if len(payload) - position < value_length:
                    raise ValueError("Incomplete batch value")

                value = payload[position:position + value_length]
                position += value_length

                operations.append(("put", key, value))

            elif op_type == DELETE:

                operations.append(("delete", key))

            else:
                raise ValueError("Unknown batch operation")

        if position != len(payload):
            raise ValueError("Extra data in batch")

        return operations

    # --------------------------------
    # APPLY OPERATIONS
    # --------------------------------

    def _apply_operations(self, operations):

        for operation in operations:

            if operation[0] == "put":
                _, key, value = operation
                self.index[key] = value

            elif operation[0] == "delete":
                _, key = operation
                self.index.pop(key, None)

    # --------------------------------
    # RECOVERY AND REFRESH
    # --------------------------------

    def _recover(self):

        # Rebuild the index from scratch
        self.index.clear()

        records = read_records(self.path)

        for record_type, payload in records:

            if record_type == PUT:

                key, value = self._decode_put(payload)
                self.index[key] = value

            elif record_type == DELETE:

                key = self._decode_delete(payload)
                self.index.pop(key, None)

            elif record_type == BATCH:

                operations = self._decode_batch(payload)
                self._apply_operations(operations)

            else:
                raise ValueError("Unknown record type")

    # --------------------------------
    # PUT
    # --------------------------------

    def put(self, key, value):

        payload = self._encode_put(key, value)

        with self.lock:
            self._ensure_open()

            with FileLock(self.lock_path):

                # Refresh data written by other processes
                self._recover()

                # Persist before updating memory
                append_record(self.path, PUT, payload)

                self.index[key] = value

    # --------------------------------
    # GET
    # --------------------------------

    def get(self, key):

        if not isinstance(key, bytes):
            raise TypeError("Key must be bytes")

        if not 1 <= len(key) <= MAX_KEY_LENGTH:
            raise ValueError("Invalid key length")

        with self.lock:
            self._ensure_open()

            with FileLock(self.lock_path):

                self._recover()

                return self.index.get(key)

    def key_count(self):
        with self.lock:
            self._ensure_open()

            with FileLock(self.lock_path):
                self._recover()
                return len(self.index)

    def items(self):
        with self.lock:
            self._ensure_open()

            with FileLock(self.lock_path):
                self._recover()
                return list(self.index.items())

    def check(self):
        with self.lock:
            self._ensure_open()

            with FileLock(self.lock_path):
                self._recover()

                return True

    # --------------------------------
    # DELETE
    # --------------------------------

    def delete(self, key):

        payload = self._encode_delete(key)

        with self.lock:
            self._ensure_open()

            with FileLock(self.lock_path):

                self._recover()

                if key not in self.index:
                    return False

                append_record(self.path, DELETE, payload)

                self.index.pop(key, None)

                return True

    # --------------------------------
    # BATCH
    # --------------------------------

    def batch(self, operations):

        payload = self._encode_batch(operations)

        if not operations:
            return True

        with self.lock:
            self._ensure_open()

            with FileLock(self.lock_path):

                self._recover()

                append_record(self.path, BATCH, payload)

                decoded = self._decode_batch(payload)
                self._apply_operations(decoded)

                return True

    # --------------------------------
    # CLOSE
    # --------------------------------

    def close(self):

        with self.lock:
            self.closed = True

    # --------------------------------
    # CONTEXT MANAGER
    # --------------------------------

    def __enter__(self):

        with self.lock:
            self._ensure_open()
            return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
