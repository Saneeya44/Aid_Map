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


def append_record(path, record_type, payload, max_bytes=None):
    record = encode_record(record_type, payload)

    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    original_size = None
    append_started = False

    try:
        original_size = os.fstat(fd).st_size
        if max_bytes is not None and original_size + len(record) > max_bytes:
            raise ValueError(
                "Database size limit exceeded (max-bytes: {})".format(max_bytes)
            )

        append_started = True
        remaining = memoryview(record)

        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise OSError("Unable to write record")
            remaining = remaining[written:]

        os.fsync(fd)

    except BaseException as append_error:
        if not append_started:
            raise

        try:
            os.ftruncate(fd, original_size)
            os.fsync(fd)
        except OSError as rollback_error:
            raise OSError(
                "Append failed ({}); rollback failed ({})".format(
                    append_error, rollback_error
                )
            ) from append_error
        raise

    finally:
        os.close(fd)


def _read_records(file, repair_incomplete):
    records = []

    while True:
        record_start = file.tell()

        # Read the header
        header = file.read(HEADER_SIZE)

        # No more records
        if not header:
            break

        # Incomplete header
        if len(header) != HEADER_SIZE:
            if not repair_incomplete:
                raise ValueError("Incomplete trailing record header")
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

        # Incomplete payload
        if len(payload) != length:
            if not repair_incomplete:
                raise ValueError("Incomplete trailing record payload")
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


def read_records(path):
    with open(path, "r+b") as file:
        return _read_records(file, repair_incomplete=True)


def verify_records(path):
    with open(path, "rb") as file:
        return _read_records(file, repair_incomplete=False)
