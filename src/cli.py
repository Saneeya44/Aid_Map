import argparse
import base64
import sys
import time


MAX_BATCH_OPERATIONS = 10000


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
                operations.append(("delete", parts[1].encode("utf-8"), None))
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
    put_parser.add_argument("path", help="database file path")
    put_parser.add_argument("key", help="key to store")
    put_parser.add_argument("value", help="value to store")

    get_parser = commands.add_parser("get", help="retrieve a value")
    get_parser.add_argument("path", help="database file path")
    get_parser.add_argument("key", help="key to retrieve")

    delete_parser = commands.add_parser("del", help="delete a value")
    delete_parser.add_argument("path", help="database file path")
    delete_parser.add_argument("key", help="key to delete")

    batch_parser = commands.add_parser("batch", help="run a batch of operations")
    batch_parser.add_argument("path", help="database file path")
    batch_parser.add_argument("batch_file", help="UTF-8 batch input file")

    open_parser = commands.add_parser("open", help="open a database and report stats")
    open_parser.add_argument("path", help="database file path")

    dump_parser = commands.add_parser(
        "dump",
        help="dump live entries as Base64 key=<...> value=<...> lines",
    )
    dump_parser.add_argument("path", help="database file path")

    check_parser = commands.add_parser("check", help="verify database integrity")
    check_parser.add_argument("path", help="database file path")

    args = parser.parse_args(argv)

    try:
        operations = None
        if args.command == "batch":
            operations = read_batch_operations(args.batch_file)

        from src.database import Database

        open_started = time.perf_counter() if args.command == "open" else None
        db = Database(args.path)

        if args.command == "open":
            open_elapsed_ms = (time.perf_counter() - open_started) * 1000
            key_count_method = getattr(db, "key_count", None)
            if not callable(key_count_method):
                raise ValueError(
                    "Database.key_count() is not available; "
                    "the storage engine must implement it"
                )
            print("Live keys: {}".format(key_count_method()))
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

        if args.command == "check":
            check_method = getattr(db, "check", None)
            if not callable(check_method):
                raise ValueError(
                    "Database.check() is not available; "
                    "the storage engine must implement it"
                )
            if check_method() is not True:
                raise ValueError("Database.check() did not confirm integrity")
            print("OK: database integrity verified")
            return 0

        if args.command == "batch":
            batch_method = getattr(db, "batch", None)
            if not callable(batch_method):
                print(
                    "Error: Database.batch() is not available; "
                    "the storage engine must implement it",
                    file=sys.stderr,
                )
                return 1
            batch_method(operations)
            print("OK")
            return 0

        key = args.key.encode("utf-8")

        if args.command == "put":
            db.put(key, args.value.encode("utf-8"))
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
