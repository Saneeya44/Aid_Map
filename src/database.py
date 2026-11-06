
import struct
import threading

from src.storage import append_record, read_records

PUT = 1


class Database:

    def __init__(self, path):
        self.path = path
        self.index = {}
        self.lock = threading.RLock()

        # Create the file if it does not exist
        with open(self.path, "ab"):
            pass

        # Reconstruct the index from stored records
        self._recover()

    def _encode_put(self, key, value):
        key_length = len(key)

        if not 1 <= key_length <= 1024:
            raise ValueError("Invalid key length")

        if len(value) > 1024 * 1024:
            raise ValueError("Value too large")

        return struct.pack(">H", key_length) + key + value

    def _decode_put(self, payload):
        if len(payload) < 2:
            raise ValueError("Invalid PUT payload")

        key_length = struct.unpack(">H", payload[:2])[0]

        if not 1 <= key_length <= 1024:
            raise ValueError("Invalid key length")

        if len(payload) < 2 + key_length:
            raise ValueError("Incomplete PUT payload")

        key = payload[2:2 + key_length]
        value = payload[2 + key_length:]

        return key, value

    def _recover(self):
        records = read_records(self.path)

        for record_type, payload in records:
            if record_type == PUT:
                key, value = self._decode_put(payload)
                self.index[key] = value
            else:
                raise ValueError("Unknown record type")

    def put(self, key, value):
        if not isinstance(key, bytes) or not isinstance(value, bytes):
            raise TypeError("Key and value must be bytes")

        payload = self._encode_put(key, value)

        with self.lock:
            # First save to disk
            append_record(self.path, PUT, payload)

            # Then update the memory index
            self.index[key] = value

    def get(self, key):
        with self.lock:
            return self.index.get(key)
