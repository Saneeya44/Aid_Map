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

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)

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

    with open(path, "r+b") as file:

        while True:
            record_start = file.tell()

            # Read the header
            header = file.read(HEADER_SIZE)

            # No more records
            if not header:
                break

            # Incomplete header: remove incomplete record
            if len(header) != HEADER_SIZE:
                file.truncate(record_start)
                file.flush()
                os.fsync(file.fileno())
                break

            magic, version, record_type, length, checksum = (
                struct.unpack(HEADER_FORMAT, header)
            )

            # Validate header
            if magic != MAGIC or version != VERSION:
                raise ValueError("Invalid record header")

            # Read payload
            payload = file.read(length)

            # Incomplete payload: remove incomplete record
            if len(payload) != length:
                file.truncate(record_start)
                file.flush()
                os.fsync(file.fileno())
                break

            # Verify checksum
            checksum_data = bytes([version, record_type]) + payload

            if zlib.crc32(checksum_data) != checksum:
                raise ValueError("Checksum mismatch")

            records.append((record_type, payload))

    return records
    