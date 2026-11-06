import argparse
import base64
import os
import sys
import time
from pathlib import Path

if __package__:
    from .database import Database
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.database import Database


MAX_BATCH_OPERATIONS = 10000


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

    if len(operands) == required_count:
        return args.db or default_database_path(), operands

    if len(operands) == required_count + 1 and args.db is None:
        return operands[0], operands[1:]

    if args.db is not None and len(operands) == required_count + 1:
        raise ValueError("Specify the database path either positionally or with --db, not both")

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
        }[args.command]
        path, operands = resolve_database_arguments(args, argument_count)

        operations = None
        if args.command == "batch":
            operations = read_batch_operations(operands[0])

        open_started = time.perf_counter() if args.command == "open" else None
        db = Database(path)

        if args.command == "open":
            open_elapsed_ms = (time.perf_counter() - open_started) * 1000
            print("Live keys: {}".format(len(db.index)))
            print(
                "Open/recovery time (includes initialization): "
                "{:.3f} ms".format(open_elapsed_ms)
            )
            return 0

        if args.command == "dump":
            for key, value in sorted(db.index.items()):
                encoded_key = base64.b64encode(key).decode("ascii")
                encoded_value = base64.b64encode(value).decode("ascii")
                print("key={} value={}".format(encoded_key, encoded_value))
            return 0

        if args.command == "check":
            # Opening the database recovers and validates its records.
            print("OK: database integrity verified")
            return 0

        if args.command == "batch":
            db.batch(operations)
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
