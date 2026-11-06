import argparse
import base64
import binascii
import json
import os
import sys
import time
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


MAX_BATCH_OPERATIONS = 10000
MAX_KEY_LENGTH = 1024
MAX_VALUE_LENGTH = 1024 * 1024


def default_database_path():
    if os.name == "nt":
        data_directory = Path(
            os.environ.get(
                "LOCALAPPDATA",
                Path.home() / "AppData" / "Local",
            )
        )
    else:
        data_directory = Path(
            os.environ.get(
                "XDG_DATA_HOME",
                Path.home() / ".local" / "share",
            )
        )

    directory = data_directory / "Aid_Map"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "aid_map.kv"


def resolve_database_arguments(args, required_count):
    operands = args.operands or []
    if isinstance(operands, str):
        operands = [operands]

    if len(operands) == required_count + 1 and args.db is None:
        return operands[0], operands[1:]

    if args.db is not None and len(operands) == required_count + 1:
        raise ValueError("Specify the database path either positionally or with --db, not both")

    if len(operands) == required_count:
        return args.db or default_database_path(), operands

    raise ValueError(
        "Expected {} command argument{}".format(
            required_count,
            "" if required_count == 1 else "s",
        )
    )


def read_batch_operations(batch_file):
    operations = []

    with open(batch_file, "r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue

            line = line.rstrip("\r\n")
            parts = line.split(None, 2)
            operation = parts[0]

            if operation == "PUT":
                if len(parts) != 3:
                    raise ValueError(
                        "Malformed PUT operation on line {}".format(line_number)
                    )
                operations.append(
                    ("put", parts[1].encode("utf-8"), parts[2].encode("utf-8"))
                )
            elif operation == "DEL":
                if len(parts) != 2:
                    raise ValueError(
                        "Malformed DEL operation on line {}".format(line_number)
                    )
                operations.append(("delete", parts[1].encode("utf-8")))
            else:
                raise ValueError(
                    "Invalid operation on line {}: {}".format(line_number, operation)
                )

            if len(operations) > MAX_BATCH_OPERATIONS:
                raise ValueError(
                    "Batch cannot exceed {} operations".format(
                        MAX_BATCH_OPERATIONS
                    )
                )

    return operations


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field: {}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("Invalid JSON constant: {}".format(value))


def _valid_operation_id(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value != "-"
        and value.isascii()
        and all(character.isprintable() and not character.isspace() for character in value)
    )


def _decode_request_bytes(value, field_name, maximum_length):
    if not isinstance(value, str):
        raise ValueError("{} must be a Base64 string".format(field_name))

    if maximum_length is not None:
        maximum_encoded_length = 4 * ((maximum_length + 2) // 3)
        if len(value) > maximum_encoded_length:
            raise ValueError("{} exceeds the maximum length".format(field_name))

    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError("{} must contain valid Base64".format(field_name)) from error

    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("{} must contain canonical Base64".format(field_name))

    if maximum_length is not None and len(decoded) > maximum_length:
        raise ValueError("{} exceeds the maximum length".format(field_name))
    return decoded


def _validate_request_fields(request, required_fields):
    missing = required_fields - request.keys()
    if missing:
        raise ValueError("Missing request field: {}".format(sorted(missing)[0]))

    extra = request.keys() - required_fields
    if extra:
        raise ValueError("Unexpected request field: {}".format(sorted(extra)[0]))


def _load_serve_json(line):
    return json.loads(
        line,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
    )


def _parse_serve_request(request):
    if not isinstance(request, dict):
        raise ValueError("Request must be a JSON object")

    operation_id = request.get("id")
    if not _valid_operation_id(operation_id):
        raise ValueError("Invalid operation id")

    operation = request.get("op")
    if operation == "put":
        _validate_request_fields(request, {"id", "op", "key", "value"})
        key = _decode_request_bytes(request["key"], "key", MAX_KEY_LENGTH)
        value = _decode_request_bytes(
            request["value"], "value", MAX_VALUE_LENGTH
        )
        if not key:
            raise ValueError("key must be at least 1 byte")
        return operation_id, ("put", key, value)

    if operation == "delete":
        _validate_request_fields(request, {"id", "op", "key"})
        key = _decode_request_bytes(request["key"], "key", MAX_KEY_LENGTH)
        if not key:
            raise ValueError("key must be at least 1 byte")
        return operation_id, ("delete", key)

    if operation == "batch":
        _validate_request_fields(request, {"id", "op", "operations"})
        request_operations = request["operations"]
        if not isinstance(request_operations, list):
            raise ValueError("operations must be a JSON array")
        if not 1 <= len(request_operations) <= MAX_BATCH_OPERATIONS:
            raise ValueError(
                "batch must contain between 1 and {} operations".format(
                    MAX_BATCH_OPERATIONS
                )
            )

        operations = []
        for index, batch_operation in enumerate(request_operations):
            if not isinstance(batch_operation, dict):
                raise ValueError(
                    "batch operation {} must be a JSON object".format(index)
                )

            operation_type = batch_operation.get("op")
            if operation_type == "put":
                _validate_request_fields(
                    batch_operation, {"op", "key", "value"}
                )
                key = _decode_request_bytes(
                    batch_operation["key"], "key", MAX_KEY_LENGTH
                )
                value = _decode_request_bytes(
                    batch_operation["value"], "value", MAX_VALUE_LENGTH
                )
                if not key:
                    raise ValueError("batch key must be at least 1 byte")
                operations.append(("put", key, value))
            elif operation_type == "delete":
                _validate_request_fields(batch_operation, {"op", "key"})
                key = _decode_request_bytes(
                    batch_operation["key"], "key", MAX_KEY_LENGTH
                )
                if not key:
                    raise ValueError("batch key must be at least 1 byte")
                operations.append(("delete", key))
            else:
                raise ValueError(
                    "Unsupported batch operation at index {}".format(index)
                )
        return operation_id, ("batch", operations)

    raise ValueError("Unsupported operation")


def _clean_error_reason(error):
    return " ".join(str(error).split()) or type(error).__name__


def serve(database_path, input_stream=None, output_stream=None, error_stream=None):
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream

    if __package__:
        from .database import Database
    else:
        from src.database import Database

    try:
        db = Database(database_path)
    except Exception as error:
        print("Error opening database: {}".format(_clean_error_reason(error)),
              file=error_stream, flush=True)
        return 1

    exit_code = 0
    try:
        for line in input_stream:
            operation_id = "-"
            try:
                request = _load_serve_json(line)
                if isinstance(request, dict) and _valid_operation_id(
                    request.get("id")
                ):
                    operation_id = request["id"]
                operation_id, operation = _parse_serve_request(request)
                operation_type = operation[0]

                if operation_type == "put":
                    _, key, value = operation
                    db.put(key, value)
                elif operation_type == "delete":
                    _, key = operation
                    db.delete(key)
                else:
                    _, operations = operation
                    if db.batch(operations) is not True:
                        raise RuntimeError("batch did not confirm success")

                response = "ACK {}".format(operation_id)
            except Exception as error:
                response = "ERR {} {}".format(
                    operation_id, _clean_error_reason(error)
                )

            output_stream.write(response + "\n")
            output_stream.flush()
    except Exception as error:
        print("Error reading SERVE input: {}".format(_clean_error_reason(error)),
              file=error_stream, flush=True)
        exit_code = 1
    finally:
        try:
            db.close()
        except Exception as error:
            print("Error closing database: {}".format(_clean_error_reason(error)),
                  file=error_stream, flush=True)
            exit_code = 1

    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Simple key-value database")
    commands = parser.add_subparsers(dest="command", required=True)

    put_parser = commands.add_parser("put", help="store a value")
    put_parser.add_argument("operands", nargs="+", metavar="ARG",
                            help="KEY VALUE (or legacy PATH KEY VALUE)")
    put_parser.add_argument("--db", help="database file path")

    get_parser = commands.add_parser("get", help="retrieve a value")
    get_parser.add_argument("operands", nargs="+", metavar="ARG",
                            help="KEY (or legacy PATH KEY)")
    get_parser.add_argument("--db", help="database file path")

    delete_parser = commands.add_parser("del", help="delete a value")
    delete_parser.add_argument("operands", nargs="+", metavar="ARG",
                               help="KEY (or legacy PATH KEY)")
    delete_parser.add_argument("--db", help="database file path")

    batch_parser = commands.add_parser("batch", help="run a batch of operations")
    batch_parser.add_argument("operands", nargs="+", metavar="ARG",
                              help="BATCH_FILE (or legacy PATH BATCH_FILE)")
    batch_parser.add_argument("--db", help="database file path")

    open_parser = commands.add_parser("open", help="open a database and report stats")
    open_parser.add_argument("operands", nargs="?", metavar="PATH",
                             help="legacy database file path")
    open_parser.add_argument("--db", help="database file path")

    dump_parser = commands.add_parser(
        "dump",
        help="dump live entries as Base64 key=<...> value=<...> lines",
    )
    dump_parser.add_argument("operands", nargs="?", metavar="PATH",
                             help="legacy database file path")
    dump_parser.add_argument("--db", help="database file path")

    check_parser = commands.add_parser("check", help="verify database integrity")
    check_parser.add_argument("operands", nargs="?", metavar="PATH",
                              help="legacy database file path")
    check_parser.add_argument("--db", help="database file path")

    serve_parser = commands.add_parser(
        "serve", help="serve newline-delimited JSON requests"
    )
    serve_parser.add_argument("operands", nargs="?", metavar="PATH",
                              help="database file path")
    serve_parser.add_argument("--db", help="database file path")

    compact_parser = commands.add_parser(
        "compact", help="reclaim space used by obsolete records"
    )
    compact_parser.add_argument("operands", nargs="?", metavar="PATH",
                                help="legacy database file path")
    compact_parser.add_argument("--db", help="database file path")

    args = parser.parse_args(argv)

    try:
        argument_count = {
            "put": 2,
            "get": 1,
            "del": 1,
            "batch": 1,
            "open": 0,
            "dump": 0,
            "check": 0,
            "serve": 0,
            "compact": 0,
        }[args.command]
        path, operands = resolve_database_arguments(args, argument_count)

        if args.command == "serve":
            return serve(path)

        operations = None
        if args.command == "batch":
            operations = read_batch_operations(operands[0])

        if __package__:
            from .database import Database
        else:
            from src.database import Database

        if args.command == "check":
            check_file = getattr(Database, "check_file", None)
            if not callable(check_file):
                raise ValueError(
                    "Database.check_file() is not available; "
                    "the storage engine must implement it"
                )
            if check_file(path) is not True:
                raise ValueError("Database.check_file() did not confirm integrity")
            print("OK: database integrity verified")
            return 0

        open_started = time.perf_counter() if args.command == "open" else None
        db = Database(path)

        if args.command == "open":
            open_elapsed_ms = (time.perf_counter() - open_started) * 1000
            key_count = getattr(db, "key_count", None)
            if not callable(key_count):
                raise ValueError(
                    "Database.key_count() is not available; "
                    "the storage engine must implement it"
                )
            live_keys = key_count()
            print("Live keys: {}".format(live_keys))
            print(
                "Open/recovery time (includes initialization): "
                "{:.3f} ms".format(open_elapsed_ms)
            )
            return 0

        if args.command == "dump":
            items_method = getattr(db, "items", None)
            if not callable(items_method):
                raise ValueError(
                    "Database.items() is not available; "
                    "the storage engine must implement it"
                )
            for key, value in sorted(items_method(), key=lambda item: item[0]):
                encoded_key = base64.b64encode(key).decode("ascii")
                encoded_value = base64.b64encode(value).decode("ascii")
                print("key={} value={}".format(encoded_key, encoded_value))
            return 0

        if args.command == "compact":
            before_bytes, after_bytes = db.compact()
            print(
                "Compacted database: {} -> {} bytes".format(
                    before_bytes, after_bytes
                )
            )
            return 0

        if args.command == "batch":
            batch_method = getattr(db, "batch", None)
            if not callable(batch_method):
                raise ValueError(
                    "Database.batch() is not available; "
                    "the storage engine must implement it"
                )
            batch_method(operations)
            print("OK")
            return 0

        key = operands[0].encode("utf-8")

        if args.command == "put":
            db.put(key, operands[1].encode("utf-8"))
            print("OK")
        elif args.command == "get":
            value = db.get(key)
            if value is None:
                print("NOTFOUND")
            else:
                print(value.decode("utf-8"))
        else:
            db.delete(key)
            print("OK")
    except (ImportError, OSError, TypeError, ValueError, RuntimeError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
