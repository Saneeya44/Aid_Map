import os
import struct
import zlib

# Record format constants
MAGIC = b"KVDB"
VERSION = 1

# PUT = 1, DELETE = 2, BATCH = 3
HEADER_FORMAT = ">4sBBII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


def encode_record(record_type, payload):
    checksum_data = bytes([VERSION, record_type]) + payload
    checksum = zlib.crc32(checksum_data)

    header = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        VERSION,
        record_type,
        len(payload),
        checksum
    )

    return header + payload


def append_record(path, record_type, payload):
    record = encode_record(record_type, payload)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    try:
        remaining = memoryview(record)

        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise OSError("Unable to write record")
            remaining = remaining[written:]

        os.fsync(fd)

    finally:
        os.close(fd)


def read_records(path):
    records = []

    with open(path, "rb") as file:
        while True:
            header = file.read(HEADER_SIZE)

            if not header:
                break

            if len(header) != HEADER_SIZE:
                break

            magic, version, record_type, length, checksum = (
                struct.unpack(HEADER_FORMAT, header)
            )

            if magic != MAGIC or version != VERSION:
                raise ValueError("Invalid record header")

            payload = file.read(length)

            if len(payload) != length:
                break

            checksum_data = bytes([version, record_type]) + payload

            if zlib.crc32(checksum_data) != checksum:
                raise ValueError("Checksum mismatch")

            records.append((record_type, payload))

    return records
