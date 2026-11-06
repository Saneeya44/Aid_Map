import argparse
import sys


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

    args = parser.parse_args(argv)

    try:
        from src.database import Database

        db = Database(args.path)
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
    except (ImportError, OSError, TypeError, ValueError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
