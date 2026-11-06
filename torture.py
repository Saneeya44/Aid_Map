import argparse
import base64
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
KNOWN_MODES = (
    "basic",
    "large-value",
    "max-batch",
    "recovery-crash",
    "compaction-crash",
    "disk-full",
)
SUPPORTED_MODES = ("basic", "large-value", "max-batch")
PROCESS_TIMEOUT = 15
READER_TIMEOUT = 10
MAX_HEAVY_ITERATIONS = 5
MAX_VALUE_LENGTH = 1024 * 1024
MAX_BATCH_OPERATIONS = 10000


class HarnessError(Exception):
    pass


def positive_integer(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser():
    parser = argparse.ArgumentParser(
        description="Exercise Aid_Map crash recovery and compare it with an independent oracle.",
        epilog=(
            "Examples:\n"
            "  python torture.py --iterations 500 --seed 12345 --mode basic\n"
            "  python torture.py --iterations 1 --seed 12345 --mode large-value\n"
            "  python torture.py --iterations 1 --seed 12345 --mode max-batch"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--iterations",
        type=positive_integer,
        default=500,
        help="number of isolated runs (default: 500; heavy modes allow at most 5)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        required=True,
        help="required seed for reproducible requests and kill delays",
    )
    parser.add_argument(
        "--mode",
        choices=KNOWN_MODES,
        default="basic",
        help="basic, maximum-value, or maximum-batch test (default: basic)",
    )
    parser.add_argument(
        "--workdir",
        help="retain artifacts in a new unique subdirectory here (default: temporary)",
    )
    return parser


def check_prerequisites(root=ROOT, python_version=None, executable=None):
    root = Path(root)
    required_files = (
        root / "src" / "cli.py",
        root / "src" / "database.py",
        root / "src" / "storage.py",
        root / "src" / "locking.py",
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise HarnessError(
            "required project files are missing: {}".format(", ".join(missing))
        )

    version = sys.version_info if python_version is None else python_version
    if tuple(version[:2]) < (3, 7):
        raise HarnessError("Python 3.7 or newer is required")

    python_executable = sys.executable if executable is None else executable
    if not python_executable or not os.path.isfile(python_executable):
        raise HarnessError(
            "Python executable was not found; run this script with an installed Python"
        )


def _create_run_root(workdir):
    if workdir is None:
        return tempfile.mkdtemp(prefix="aid-map-torture-"), True
    parent = os.path.abspath(workdir)
    os.makedirs(parent, exist_ok=True)
    return tempfile.mkdtemp(prefix="aid-map-torture-", dir=parent), False


def _cleanup_run_root(run_root, temporary, preserve_artifacts):
    if temporary and not preserve_artifacts:
        import shutil
        shutil.rmtree(run_root)


def _safe_print(*values, **kwargs):
    rendered = [
        str(value).encode("ascii", "backslashreplace").decode("ascii")
        for value in values
    ]
    print(*rendered, **kwargs)


def _print_banner(mode, seed, iterations, run_root, temporary):
    writers = 2 if mode == "basic" else 1
    retention = "removed after complete PASS; retained otherwise" if temporary else "retained"
    _safe_print("Aid_Map Crash Torture")
    _safe_print("Mode: {}".format(mode))
    _safe_print("Seed: {}".format(seed))
    _safe_print("Iterations: {}".format(iterations))
    _safe_print("Writer count: {}".format(writers))
    _safe_print("Independent oracle: enabled (fsynced JSONL)")
    _safe_print("Database/artifact location: {}".format(run_root))
    _safe_print("Retention: {}".format(retention))


def _print_iteration(number, total, result, mode=None):
    acked = sum(
        response[0] == "ACK"
        for response in result.get("statuses", {}).values()
    )
    sent = result.get("sent_count", len(result.get("attempts", [])))
    pending = result.get(
        "pending_at_kill",
        result.get("protocol_pending_at_kill", False),
    )
    delay = result.get("kill_delay_ms", "n/a")
    reason = " ".join(str(result.get("reason", "no result")).split())
    if len(reason) > 120:
        reason = reason[:117] + "..."
    label = "ITER {}/{} {}".format(number, total, result["result"])
    if mode:
        label += " mode={}".format(mode)
    _safe_print(
        "{} delay={}ms ACKed/sent={}/{} pending_at_kill={} verification={}".format(
            label,
            delay,
            acked,
            sent,
            "yes" if pending else "no",
            reason,
        ),
        flush=True,
    )


def _print_summary(passed, failed, inconclusive, seed, started_at, verdict, errors=0):
    elapsed = time.monotonic() - started_at
    _safe_print(
        "SUMMARY PASS={} FAIL={} INCONCLUSIVE={} ERROR={} seed={} "
        "elapsed={:.2f}s verdict={}".format(
            passed, failed, inconclusive, errors, seed, elapsed, verdict
        ),
        flush=True,
    )
    return elapsed


def _write_failure_metadata(directory, metadata):
    path = Path(directory) / "failure.json"
    with open(path, "w", encoding="ascii") as file:
        json.dump(metadata, file, sort_keys=True, indent=2, ensure_ascii=True)
        file.write("\n")
    return path


def parse_response(line):
    line = line.rstrip("\r\n")
    parts = line.split(" ", 2)
    if len(parts) < 2:
        raise ValueError("Malformed SERVE response")

    response_type, operation_id = parts[0], parts[1]
    if response_type == "ACK" and len(parts) == 2:
        return response_type, operation_id, ""
    if response_type == "ERR" and len(parts) == 3 and parts[2]:
        return response_type, operation_id, parts[2]
    raise ValueError("Malformed SERVE response")


def decode_dump(text):
    entries = {}
    if not text:
        return entries

    for line_number, line in enumerate(text.splitlines(), start=1):
        match = re.fullmatch(r"key=([A-Za-z0-9+/]*={0,2}) value=([A-Za-z0-9+/]*={0,2})", line)
        if match is None:
            raise ValueError("Malformed dump line {}".format(line_number))
        key_text, value_text = match.groups()
        key = base64.b64decode(key_text, validate=True)
        value = base64.b64decode(value_text, validate=True)
        if base64.b64encode(key).decode("ascii") != key_text:
            raise ValueError("Non-canonical key Base64 on dump line {}".format(line_number))
        if base64.b64encode(value).decode("ascii") != value_text:
            raise ValueError(
                "Non-canonical value Base64 on dump line {}".format(line_number)
            )
        if key in entries:
            raise ValueError("Duplicate key in dump")
        entries[key] = value
    return entries


def _decode_oracle_bytes(value, field_name, maximum_length):
    if not isinstance(value, str):
        raise ValueError("{} is not a Base64 string".format(field_name))
    decoded = base64.b64decode(value.encode("ascii"), validate=True)
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("{} is not canonical Base64".format(field_name))
    if len(decoded) > maximum_length:
        raise ValueError("{} exceeds the protocol limit".format(field_name))
    return decoded


def _valid_oracle_id(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value != "-"
        and value.isascii()
        and all(character.isprintable() and not character.isspace() for character in value)
    )


def _validated_operation(operation):
    if not isinstance(operation, dict):
        raise ValueError("Operation is not an object")

    operation_type = operation.get("op")
    if operation_type == "put":
        if set(operation) != {"op", "key", "value"}:
            raise ValueError("PUT operation has invalid fields")
        key = _decode_oracle_bytes(operation["key"], "key", 1024)
        value = _decode_oracle_bytes(operation["value"], "value", 1024 * 1024)
        if not key:
            raise ValueError("PUT key is empty")
        return "put", key, value

    if operation_type == "delete":
        if set(operation) != {"op", "key"}:
            raise ValueError("DELETE operation has invalid fields")
        key = _decode_oracle_bytes(operation["key"], "key", 1024)
        if not key:
            raise ValueError("DELETE key is empty")
        return "delete", key

    raise ValueError("Unsupported oracle operation: {}".format(operation_type))


def apply_request(state, request):
    if not isinstance(request, dict):
        raise ValueError("Request is not an object")

    operation = request.get("op")
    if operation == "put":
        if set(request) != {"id", "op", "key", "value"}:
            raise ValueError("PUT request has invalid fields")
        validated = _validated_operation({
            "op": operation,
            "key": request["key"],
            "value": request["value"],
        })
        state[validated[1]] = validated[2]
    elif operation == "delete":
        if set(request) != {"id", "op", "key"}:
            raise ValueError("DELETE request has invalid fields")
        validated = _validated_operation({
            "op": operation,
            "key": request["key"],
        })
        state.pop(validated[1], None)
    elif operation == "batch":
        if set(request) != {"id", "op", "operations"}:
            raise ValueError("BATCH request has invalid fields")
        operations = request["operations"]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 10000:
            raise ValueError("BATCH request has an invalid operation count")

        # Validate the whole atomic request before applying any item.
        validated_operations = [_validated_operation(item) for item in operations]
        for item in validated_operations:
            if item[0] == "put":
                state[item[1]] = item[2]
            else:
                state.pop(item[1], None)
    else:
        raise ValueError("Unsupported oracle operation: {}".format(operation))


def verify_oracle(attempts, responses, actual_state):
    """Verify against the exact sent order; the initial database is empty."""
    if not isinstance(actual_state, dict) or any(
        not isinstance(key, bytes) or not isinstance(value, bytes)
        for key, value in actual_state.items()
    ):
        return "INCONCLUSIVE", [], "recovered state is not a bytes-to-bytes mapping"

    id_to_index = {}
    for index, event in enumerate(attempts):
        if not isinstance(event, dict) or event.get("sequence") != index:
            return "INCONCLUSIVE", [], "request history has an invalid sequence"
        request = event.get("request")
        if not isinstance(request, dict):
            return "INCONCLUSIVE", [], "request history contains an invalid request"
        operation_id = request.get("id")
        if not _valid_oracle_id(operation_id):
            return "INCONCLUSIVE", [], "request history contains an invalid ID"
        if operation_id in id_to_index:
            return "INCONCLUSIVE", [], "duplicate operation ID {}".format(operation_id)
        id_to_index[operation_id] = index

    if not isinstance(responses, dict):
        return "INCONCLUSIVE", [], "response history is invalid"

    for operation_id, response in responses.items():
        if (
            not isinstance(operation_id, str)
            or not isinstance(response, (list, tuple))
            or len(response) != 2
        ):
            return "INCONCLUSIVE", [], "response history is malformed"
        response_type, reason = response
        if operation_id not in id_to_index:
            return "INCONCLUSIVE", [], "response for unknown operation id {}".format(
                operation_id
            )
        if response_type not in ("ACK", "ERR") or not isinstance(reason, str):
            return "INCONCLUSIVE", [], "invalid response for {}".format(operation_id)
        if (
            (response_type == "ACK" and reason)
            or (response_type == "ERR" and not reason)
        ):
            return "INCONCLUSIVE", [], "invalid response reason for {}".format(
                operation_id
            )
        if response_type == "ERR":
            return "INCONCLUSIVE", [], "database returned ERR: {}".format(reason)

    try:
        acknowledged = sorted(
            id_to_index[operation_id]
            for operation_id, (response_type, _) in responses.items()
            if response_type == "ACK"
        )
    except (TypeError, ValueError):
        return "INCONCLUSIVE", [], "response history is malformed"
    ack_prefix = len(acknowledged)
    if acknowledged != list(range(ack_prefix)):
        return "INCONCLUSIVE", [], "ACKs do not form the serialized request prefix"
    if ack_prefix > len(attempts):
        return "INCONCLUSIVE", [], "ACK count exceeds attempted requests"

    # SERVE processes its input loop serially. All ACKed operations are durable;
    # after them, at most one operation can be in flight when the process dies.
    prefix_lengths = {ack_prefix}
    if ack_prefix < len(attempts):
        prefix_lengths.add(ack_prefix + 1)

    candidates = []
    try:
        for prefix_length in sorted(prefix_lengths):
            state = {}
            for event in attempts[:prefix_length]:
                apply_request(state, event["request"])
            if state not in candidates:
                candidates.append(state)
    except (KeyError, TypeError, ValueError, UnicodeEncodeError):
        return "INCONCLUSIVE", [], "request history contains invalid operation data"

    if actual_state in candidates:
        return "PASS", candidates, "recovered state matches an allowed serialized prefix"
    return "FAIL", candidates, "recovered state matches no allowed serialized prefix"


def verify_oracle_events(events, actual_state):
    """Verify using request, successful-send, and response events from the log."""
    if not isinstance(events, list):
        return "INCONCLUSIVE", [], "oracle log is not an event list"

    requests = {}
    sent = {}
    responses = {}
    for event in events:
        if not isinstance(event, dict):
            return "INCONCLUSIVE", [], "oracle log contains a malformed event"
        event_type = event.get("event")
        operation_id = event.get("id")
        if event_type == "request":
            request = event.get("request")
            sequence = event.get("sequence")
            operation_id = request.get("id") if isinstance(request, dict) else None
            if (
                not isinstance(request, dict)
                or not _valid_oracle_id(operation_id)
                or request.get("id") != operation_id
                or not isinstance(sequence, int)
                or sequence < 0
                or operation_id in requests
            ):
                return "INCONCLUSIVE", [], "oracle log contains an invalid request event"
            requests[operation_id] = (sequence, request)
        elif event_type == "sent":
            sequence = event.get("sequence")
            if (
                not isinstance(operation_id, str)
                or not isinstance(sequence, int)
                or operation_id in sent
            ):
                return "INCONCLUSIVE", [], "oracle log contains an invalid sent event"
            sent[operation_id] = sequence
        elif event_type == "response":
            response_type = event.get("response")
            reason = event.get("reason")
            if (
                not isinstance(operation_id, str)
                or response_type not in ("ACK", "ERR")
                or not isinstance(reason, str)
                or (response_type == "ACK" and reason)
                or (response_type == "ERR" and not reason)
                or operation_id in responses
            ):
                return "INCONCLUSIVE", [], "oracle log contains an invalid response event"
            responses[operation_id] = (response_type, reason)
        elif event_type == "send_error":
            return "INCONCLUSIVE", [], "a request write to SERVE failed"
        elif event_type not in ("kill", "verification"):
            return "INCONCLUSIVE", [], "oracle log contains an unknown event"

    if set(requests) != set(sent):
        return "INCONCLUSIVE", [], "not every logged request has a confirmed send"
    if any(operation_id not in requests for operation_id in responses):
        return "INCONCLUSIVE", [], "response exists for an unknown request"

    ordered = sorted(
        ((sequence, operation_id, request) for operation_id, (sequence, request) in requests.items()),
        key=lambda item: item[0],
    )
    if [item[0] for item in ordered] != list(range(len(ordered))):
        return "INCONCLUSIVE", [], "oracle request sequence has gaps or duplicates"
    for sequence, operation_id, _ in ordered:
        if sent[operation_id] != sequence:
            return "INCONCLUSIVE", [], "sent order differs from request order"

    attempts = [
        {"sequence": sequence, "request": request}
        for sequence, _, request in ordered
    ]
    return verify_oracle(attempts, responses, actual_state)


def verify_oracle_log(path, actual_state):
    try:
        with open(path, "r", encoding="utf-8") as file:
            events = [json.loads(line) for line in file]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return "INCONCLUSIVE", [], "could not read durable oracle log: {}".format(error)
    return verify_oracle_events(events, actual_state)


class OracleLog:
    def __init__(self, path):
        self.path = path
        self.file = open(path, "x", encoding="utf-8")
        self.lock = threading.Lock()

    def record(self, event):
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with self.lock:
            self.file.write(encoded + "\n")
            self.file.flush()
            os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


def _environment():
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT) if not existing else str(ROOT) + os.pathsep + existing
    )
    return environment


def run_cli(command, database_path):
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli",
                command,
                str(database_path),
            ],
            cwd=str(ROOT),
            env=_environment(),
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise HarnessError(
            "CLI {} timed out after {} seconds".format(command, PROCESS_TIMEOUT)
        ) from error


def read_recovered_dump(database_path):
    open_result = run_cli("open", database_path)
    if open_result.returncode != 0:
        raise HarnessError(
            "CLI open failed: {}".format(
                (open_result.stderr or open_result.stdout).strip()
            )
        )

    check_result = run_cli("check", database_path)
    if (
        check_result.returncode != 0
        or check_result.stdout != "OK: database integrity verified\n"
    ):
        raise HarnessError(
            "CLI check failed: {}".format(
                (check_result.stderr or check_result.stdout).strip()
            )
        )

    dump_result = run_cli("dump", database_path)
    if dump_result.returncode != 0:
        raise HarnessError(
            "CLI dump failed: {}".format(
                (dump_result.stderr or dump_result.stdout).strip()
            )
        )
    try:
        return decode_dump(dump_result.stdout)
    except ValueError as error:
        raise HarnessError("Could not parse CLI dump: {}".format(error)) from error


def random_value(generator):
    return bytes(generator.randrange(256) for _ in range(generator.randrange(0, 25)))


def make_operation(generator, operation_index, writer_index):
    operation = generator.choices(
        ("put", "delete", "batch"),
        weights=(5, 2, 3),
        k=1,
    )[0]
    shared_keys = [
        b"hot-0",
        b"hot-1",
        b"hot-2",
        b"hot-3",
        b"hot-4",
        b"hot-5",
        ("distinct-{}-{}".format(writer_index, operation_index)).encode("ascii"),
    ]

    def make_item():
        key = generator.choice(shared_keys)
        if generator.random() < 0.6:
            return {"op": "put", "key": _b64(key), "value": _b64(random_value(generator))}
        return {"op": "delete", "key": _b64(key)}

    if operation == "put":
        item = make_item()
        if item["op"] == "delete":
            item["value"] = _b64(random_value(generator))
        item["op"] = "put"
        request = {
            "id": None,
            "op": "put",
            "key": item["key"],
            "value": item["value"],
        }
    elif operation == "delete":
        request = {
            "id": None,
            "op": "delete",
            "key": _b64(generator.choice(shared_keys)),
        }
    else:
        count = generator.randint(1, 4)
        request = {
            "id": None,
            "op": "batch",
            "operations": [make_item() for _ in range(count)],
        }
    return request


def _b64(value):
    return base64.b64encode(value).decode("ascii")


def make_large_value_scenarios(generator):
    key = b"large-value-key"
    first_byte = generator.randrange(256)
    overwrite_byte = (first_byte + 1) % 256
    delete_byte = (overwrite_byte + 1) % 256
    first_value = bytes([first_byte]) * MAX_VALUE_LENGTH
    overwrite_value = bytes([overwrite_byte]) * MAX_VALUE_LENGTH
    delete_value = bytes([delete_byte]) * MAX_VALUE_LENGTH
    return [
        (
            "large-put",
            [{
                "id": None,
                "op": "put",
                "key": _b64(key),
                "value": _b64(first_value),
            }],
        ),
        (
            "large-overwrite",
            [
                {
                    "id": None,
                    "op": "put",
                    "key": _b64(key),
                    "value": _b64(first_value),
                },
                {
                    "id": None,
                    "op": "put",
                    "key": _b64(key),
                    "value": _b64(overwrite_value),
                },
            ],
        ),
        (
            "large-delete",
            [
                {
                    "id": None,
                    "op": "put",
                    "key": _b64(key),
                    "value": _b64(delete_value),
                },
                {
                    "id": None,
                    "op": "delete",
                    "key": _b64(key),
                },
            ],
        ),
    ]


def make_max_batch_request(generator):
    key_count = 128
    operations = []
    for index in range(MAX_BATCH_OPERATIONS):
        key = ("batch-key-{:03d}".format(index % key_count)).encode("ascii")
        if index == 0 or (index > 1 and generator.randrange(2)):
            value = ("value-{:05d}".format(index)).encode("ascii")
            operations.append({
                "op": "put",
                "key": _b64(key),
                "value": _b64(value),
            })
        else:
            operations.append({
                "op": "delete",
                "key": _b64(key),
            })
    return {
        "id": None,
        "op": "batch",
        "operations": operations,
    }


class IterationRunner:
    def __init__(self, directory, iteration, generator):
        self.directory = Path(directory)
        self.iteration = iteration
        self.database_path = self.directory / "database.kv"
        self.oracle = OracleLog(str(self.directory / "oracle.jsonl"))
        self.generator = generator
        self.status_lock = threading.Lock()
        self.statuses = {}
        self.response_condition = threading.Condition(self.status_lock)
        self.reader_errors = []
        self.stderr_lines = []
        self.sent_condition = threading.Condition()
        self.sent_count = 0
        self.attempts = []
        self.sent_ids = []
        self.attempt_lock = threading.Lock()
        self.sequence_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active_lock = threading.Lock()
        self.active_writers = 0
        self.first_sent = threading.Event()
        self.target_sent = threading.Event()
        self.target_operation_id = None
        self.writer_errors = []
        self.process = None
        self.writers = []
        self.closed = False

    def _record_response(self, line):
        try:
            response_type, operation_id, reason = parse_response(line)
        except ValueError as error:
            self.reader_errors.append("{}: {!r}".format(error, line))
            return

        with self.status_lock:
            if operation_id in self.statuses:
                self.reader_errors.append(
                    "duplicate response for {}".format(operation_id)
                )
            else:
                self.oracle.record({
                    "event": "response",
                    "id": operation_id,
                    "response": response_type,
                    "reason": reason,
                })
                self.statuses[operation_id] = (response_type, reason)
                self.response_condition.notify_all()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                self._record_response(line)
        except Exception as error:
            self.reader_errors.append("stdout reader: {}".format(error))

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                self.stderr_lines.append(line.rstrip("\r\n"))
        except Exception as error:
            self.reader_errors.append("stderr reader: {}".format(error))

    def _start_server(self):
        command = [
            sys.executable,
            "-u",
            "-m",
            "src.cli",
            "serve",
            str(self.database_path),
        ]
        self.process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

        deadline = time.monotonic() + PROCESS_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise HarnessError(
                    "SERVE exited during startup: {}".format(
                        "\n".join(self.stderr_lines)
                    )
                )
            if self.database_path.exists() and Path(
                str(self.database_path) + ".lock"
            ).exists():
                return
            time.sleep(0.01)
        raise HarnessError("Timed out waiting for SERVE database initialization")

    def _writer(self, writer_index, requests, delays, start_barrier):
        with self.active_lock:
            self.active_writers += 1
        try:
            start_barrier.wait(timeout=READER_TIMEOUT)
            for request, delay in zip(requests, delays):
                if self.stop_event.is_set():
                    break
                with self.send_lock:
                    if self.stop_event.is_set():
                        break
                    sequence = len(self.attempts)
                    request["id"] = "i{}-w{}-op{}".format(
                        self.iteration,
                        writer_index,
                        len(self.attempts_for_writer[writer_index]),
                    )
                    event = {
                        "event": "request",
                        "sequence": sequence,
                        "writer": writer_index,
                        "request": request,
                    }
                    self.oracle.record(event)
                    self.attempts.append(event)
                    self.attempts_for_writer[writer_index].append(event)
                    try:
                        self.process.stdin.write(
                            json.dumps(request, separators=(",", ":")) + "\n"
                        )
                        self.process.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError) as error:
                        self.oracle.record({
                            "event": "send_error",
                            "id": request["id"],
                            "reason": str(error),
                        })
                        self.writer_errors.append(
                            "send {}: {}".format(request["id"], error)
                        )
                        self.stop_event.set()
                        break

                    self.oracle.record({
                        "event": "sent",
                        "id": request["id"],
                        "sequence": sequence,
                    })
                    self.sent_ids.append(request["id"])
                    self.sent_count += 1
                    self.first_sent.set()
                time.sleep(delay)
        except Exception as error:
            self.writer_errors.append(
                "writer {}: {}".format(writer_index, error)
            )
            self.stop_event.set()
        finally:
            with self.active_lock:
                self.active_writers -= 1

    def _stress_writer(self, requests, target_index):
        self.attempts_for_writer = [[]]
        try:
            for request_index, request in enumerate(requests[:target_index + 1]):
                if self.stop_event.is_set():
                    return

                with self.send_lock:
                    sequence = len(self.attempts)
                    operation_id = "i{}-stress-op{}".format(
                        self.iteration, request_index
                    )
                    request["id"] = operation_id
                    event = {
                        "event": "request",
                        "sequence": sequence,
                        "writer": 0,
                        "request": request,
                    }
                    self.oracle.record(event)
                    self.attempts.append(event)
                    self.attempts_for_writer[0].append(event)
                    try:
                        self.process.stdin.write(
                            json.dumps(request, separators=(",", ":")) + "\n"
                        )
                        self.process.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError) as error:
                        self.oracle.record({
                            "event": "send_error",
                            "id": operation_id,
                            "reason": str(error),
                        })
                        self.writer_errors.append(
                            "send {}: {}".format(operation_id, error)
                        )
                        self.stop_event.set()
                        return

                    self.oracle.record({
                        "event": "sent",
                        "id": operation_id,
                        "sequence": sequence,
                    })
                    self.sent_ids.append(operation_id)
                    self.sent_count += 1
                    self.first_sent.set()
                    if request_index == target_index:
                        self.target_operation_id = operation_id
                        self.target_sent.set()
                        return

                deadline = time.monotonic() + PROCESS_TIMEOUT
                with self.response_condition:
                    while operation_id not in self.statuses:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.writer_errors.append(
                                "timed out waiting for ACK {}".format(operation_id)
                            )
                            self.stop_event.set()
                            return
                        self.response_condition.wait(timeout=remaining)
                    response_type, reason = self.statuses[operation_id]
                if response_type != "ACK":
                    self.writer_errors.append(
                        "precondition {} returned ERR: {}".format(
                            operation_id, reason
                        )
                    )
                    self.stop_event.set()
                    return
        except Exception as error:
            self.writer_errors.append("stress writer: {}".format(error))
            self.stop_event.set()

    def run_stress(self, requests, target_index, kill_delay):
        if not requests or not 0 <= target_index < len(requests):
            raise ValueError("Stress request target is out of range")

        self._start_server()
        writer = threading.Thread(
            target=self._stress_writer,
            args=(requests, target_index),
            daemon=True,
        )
        self.writers = [writer]
        writer.start()

        if not self.target_sent.wait(timeout=PROCESS_TIMEOUT):
            raise HarnessError(
                "Timed out waiting for stress operation to be sent: {}".format(
                    "; ".join(self.writer_errors)
                )
            )

        time.sleep(kill_delay)
        with self.status_lock:
            target_id = self.target_operation_id
            pending_at_kill = target_id not in self.statuses
            server_alive_at_kill = self.process.poll() is None
            self.oracle.record({
                "event": "kill",
                "delay_ms": round(kill_delay * 1000, 3),
                "server_alive": server_alive_at_kill,
                "pending_at_kill": pending_at_kill,
                "pending_operation_id": target_id if pending_at_kill else None,
            })
            if server_alive_at_kill:
                try:
                    self.process.kill()
                except OSError:
                    if self.process.poll() is None:
                        raise

        self._stop_and_reap()
        if writer.is_alive():
            raise HarnessError("Stress writer did not stop after force-kill")

        if self.reader_errors:
            result = "INCONCLUSIVE"
            candidates = []
            explanation = "; ".join(self.reader_errors)
            actual_state = None
        else:
            actual_state = read_recovered_dump(self.database_path)
            with self.status_lock:
                statuses = dict(self.statuses)
            result, candidates, explanation = verify_oracle_log(
                self.oracle.path,
                actual_state,
            )
            if self.writer_errors:
                result = "INCONCLUSIVE"
                explanation = "writer outcome uncertain: {}".format(
                    "; ".join(self.writer_errors)
                )
            elif not server_alive_at_kill and result != "FAIL":
                result = "INCONCLUSIVE"
                explanation = "SERVE had exited before the force-kill"
            elif not pending_at_kill and result != "FAIL":
                result = "INCONCLUSIVE"
                explanation = (
                    "target operation had already been acknowledged at kill time"
                )

        with self.status_lock:
            statuses = dict(self.statuses)
        self.oracle.record({
            "event": "verification",
            "result": result,
            "reason": explanation,
            "acknowledged_ids": sorted(
                operation_id
                for operation_id, response in statuses.items()
                if response[0] == "ACK"
            ),
            "candidate_states": [
                _format_state(state) for state in candidates
            ],
        })
        return {
            "result": result,
            "reason": explanation,
            "kill_delay_ms": round(kill_delay * 1000, 3),
            "server_alive_at_kill": server_alive_at_kill,
            "pending_at_kill": pending_at_kill,
            "target_operation_id": target_id,
            "attempts": self.attempts,
            "statuses": statuses,
            "candidates": candidates,
            "actual": actual_state,
        }

    def _stop_and_reap(self):
        self.stop_event.set()
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                if self.process.poll() is None:
                    raise
        try:
            self.process.wait(timeout=PROCESS_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise HarnessError("Could not reap killed SERVE process") from error
        for writer in self.writers:
            writer.join(timeout=READER_TIMEOUT)
            if writer.is_alive():
                raise HarnessError("Writer thread did not stop after force-kill")
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError as error:
                self.writer_errors.append(
                    "closing SERVE stdin after force-kill: {}".format(error)
                )
        self.stdout_thread.join(timeout=READER_TIMEOUT)
        self.stderr_thread.join(timeout=READER_TIMEOUT)
        if self.stdout_thread.is_alive() or self.stderr_thread.is_alive():
            raise HarnessError("Timed out waiting for SERVE output readers")

    def run(self):
        self._start_server()
        requests = []
        for writer_index in range(2):
            requests.append([
                make_operation(
                    self.generator,
                    request_index,
                    writer_index,
                )
                for request_index in range(self.generator.randint(12, 18))
            ])
        self.attempts_for_writer = [[], []]
        writer_delays = [
            [self.generator.uniform(0.004, 0.012) for _ in writer_requests]
            for writer_requests in requests
        ]
        kill_delay_ms = self.generator.randint(2, 30)

        barrier = threading.Barrier(3)
        writers = [
            threading.Thread(
                target=self._writer,
                args=(index, writer_requests, writer_delays[index], barrier),
                daemon=True,
            )
            for index, writer_requests in enumerate(requests)
        ]
        self.writers = writers
        for writer in writers:
            writer.start()
        barrier.wait(timeout=READER_TIMEOUT)

        if not self.first_sent.wait(timeout=PROCESS_TIMEOUT):
            raise HarnessError("No writer successfully sent a request")

        time.sleep(kill_delay_ms / 1000)
        with self.active_lock:
            active_at_kill = self.active_writers > 0
        with self.send_lock:
            with self.status_lock:
                pending_ids = [
                    operation_id
                    for operation_id in self.sent_ids
                    if operation_id not in self.statuses
                ]
                server_alive_at_kill = self.process.poll() is None
                self.oracle.record({
                    "event": "kill",
                    "delay_ms": kill_delay_ms,
                    "writers_active": active_at_kill,
                    "server_alive": server_alive_at_kill,
                    "pending_at_kill": bool(pending_ids),
                    "pending_operation_ids": pending_ids,
                })
                if server_alive_at_kill:
                    try:
                        self.process.kill()
                    except OSError:
                        if self.process.poll() is None:
                            raise
        self._stop_and_reap()

        for writer in writers:
            writer.join(timeout=READER_TIMEOUT)
            if writer.is_alive():
                raise HarnessError("Writer thread did not stop after force-kill")

        if self.reader_errors:
            return {
                "result": "INCONCLUSIVE",
                "reason": "; ".join(self.reader_errors),
                "kill_delay_ms": kill_delay_ms,
                "active_at_kill": active_at_kill,
                "pending_at_kill": bool(pending_ids),
                "protocol_pending_at_kill": bool(pending_ids),
                "server_alive_at_kill": server_alive_at_kill,
                "sent_count": len(self.sent_ids),
                "statuses": dict(self.statuses),
                "attempts": self.attempts,
                "candidates": [],
                "actual": None,
            }

        actual_state = read_recovered_dump(self.database_path)
        with self.status_lock:
            statuses = dict(self.statuses)
        result, candidates, explanation = verify_oracle_log(
            self.oracle.path,
            actual_state,
        )
        if self.writer_errors:
            result = "INCONCLUSIVE"
            explanation = "writer pipe outcome uncertain: {}".format(
                "; ".join(self.writer_errors)
            )
        if not active_at_kill and result != "FAIL":
            result = "INCONCLUSIVE"
            explanation = "force-kill occurred after writers were no longer active"
        elif not server_alive_at_kill and result != "FAIL":
            result = "INCONCLUSIVE"
            explanation = "SERVE had exited before the force-kill"

        self.oracle.record({
            "event": "verification",
            "result": result,
            "reason": explanation,
            "acknowledged_ids": sorted(
                operation_id
                for operation_id, response in statuses.items()
                if response[0] == "ACK"
            ),
            "candidate_states": [
                {
                    base64.b64encode(key).decode("ascii"):
                    base64.b64encode(value).decode("ascii")
                    for key, value in sorted(state.items())
                }
                for state in candidates
            ],
        })
        return {
            "result": result,
            "reason": explanation,
            "kill_delay_ms": kill_delay_ms,
            "active_at_kill": active_at_kill,
            "pending_at_kill": bool(pending_ids),
            "protocol_pending_at_kill": bool(pending_ids),
            "server_alive_at_kill": server_alive_at_kill,
            "sent_count": len(self.sent_ids),
            "attempts": self.attempts,
            "statuses": statuses,
            "candidates": candidates,
            "actual": actual_state,
            "stderr": self.stderr_lines,
        }

    def close(self):
        if self.closed:
            return
        self.stop_event.set()
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    try:
                        self.process.kill()
                    except OSError:
                        if self.process.poll() is None:
                            raise
                self.process.wait(timeout=PROCESS_TIMEOUT)
                for writer in self.writers:
                    writer.join(timeout=READER_TIMEOUT)
                    if writer.is_alive():
                        raise HarnessError("Writer thread did not stop during cleanup")
                if self.process.stdin and not self.process.stdin.closed:
                    try:
                        self.process.stdin.close()
                    except OSError as error:
                        self.writer_errors.append(
                            "closing SERVE stdin during cleanup: {}".format(error)
                        )
                for thread_name in ("stdout_thread", "stderr_thread"):
                    thread = getattr(self, thread_name, None)
                    if thread is not None:
                        thread.join(timeout=READER_TIMEOUT)
                        if thread.is_alive():
                            raise HarnessError(
                                "Output reader did not stop during cleanup"
                            )
                for stream_name in ("stdout", "stderr"):
                    stream = getattr(self.process, stream_name, None)
                    if stream is not None and not stream.closed:
                        stream.close()
        finally:
            self.oracle.close()
            self.closed = True


def _format_state(state):
    return {
        base64.b64encode(key).decode("ascii"):
        base64.b64encode(value).decode("ascii")
        for key, value in sorted(state.items())
    }


def _exit_code(passed, failed, inconclusive, errors=0):
    if errors:
        return 3
    if failed:
        return 1
    if inconclusive or not passed:
        return 2
    return 0


def _failure_metadata(seed, mode, iteration, result, error=None):
    return {
        "seed": seed,
        "mode": mode,
        "iteration": iteration,
        "result": result.get("result", "ERROR"),
        "reason": str(error if error is not None else result.get("reason", "")),
        "kill_delay_ms": result.get("kill_delay_ms"),
        "pending_at_kill": result.get("pending_at_kill"),
        "protocol_pending_at_kill": result.get("protocol_pending_at_kill"),
        "operation_ids": [
            event.get("request", {}).get("id")
            for event in result.get("attempts", [])
            if isinstance(event.get("request"), dict)
        ],
        "expected_candidates": [
            _format_state(state)
            for state in result.get("candidates", [])
        ],
        "actual_state": (
            _format_state(result["actual"])
            if isinstance(result.get("actual"), dict)
            else None
        ),
    }


def _report_failure(result, seed, iteration, mode, directory, error=None):
    metadata_path = _write_failure_metadata(
        directory,
        _failure_metadata(seed, mode, iteration, result, error),
    )
    _safe_print(
        "Evidence retained: {} (database, oracle.jsonl, failure.json)".format(
            directory
        ),
        flush=True,
    )
    _safe_print("Failure metadata: {}".format(metadata_path), flush=True)


def run_basic(iterations, seed, workdir=None):
    started_at = time.monotonic()
    generator = random.Random(seed)
    run_root, temporary = _create_run_root(workdir)
    _print_banner("basic", seed, iterations, run_root, temporary)

    passed = failed = inconclusive = errors = 0
    preserve_artifacts = not temporary
    try:
        for iteration in range(1, iterations + 1):
            iteration_directory = os.path.join(
                run_root, "iteration-{:06d}".format(iteration)
            )
            os.mkdir(iteration_directory)
            runner = IterationRunner(iteration_directory, iteration, generator)
            result = None
            infrastructure_error = None
            try:
                result = runner.run()
            except Exception as error:
                infrastructure_error = error
                result = {
                    "result": "ERROR",
                    "reason": "harness/environment error: {}".format(error),
                    "attempts": runner.attempts,
                    "statuses": dict(runner.statuses),
                    "sent_count": len(runner.sent_ids),
                    "candidates": [],
                    "actual": None,
                    "kill_delay_ms": None,
                    "pending_at_kill": None,
                }
            finally:
                try:
                    runner.close()
                except Exception as error:
                    if infrastructure_error is None:
                        infrastructure_error = error
                        result = {
                            "result": "ERROR",
                            "reason": "harness/environment cleanup error: {}".format(error),
                            "attempts": runner.attempts,
                            "statuses": dict(runner.statuses),
                            "sent_count": len(runner.sent_ids),
                            "candidates": [],
                            "actual": None,
                            "kill_delay_ms": None,
                            "pending_at_kill": None,
                        }

            if result["result"] == "PASS":
                passed += 1
            elif result["result"] == "FAIL":
                failed += 1
                preserve_artifacts = True
            elif result["result"] == "INCONCLUSIVE":
                inconclusive += 1
                preserve_artifacts = True
            else:
                errors += 1
                preserve_artifacts = True

            if result["result"] != "PASS":
                try:
                    _report_failure(
                        result,
                        seed,
                        iteration,
                        "basic",
                        iteration_directory,
                        infrastructure_error,
                    )
                except OSError as error:
                    errors += 1
                    result["reason"] = "{}; unable to write failure metadata: {}".format(
                        result["reason"], error
                    )
            _print_iteration(iteration, iterations, result)

            if result["result"] in ("FAIL", "ERROR"):
                break

        verdict = (
            "ERROR" if errors else
            "FAIL" if failed else
            "INCONCLUSIVE" if inconclusive else
            "PASS" if passed == iterations else
            "ERROR"
        )
        _print_summary(
            passed, failed, inconclusive, seed, started_at, verdict, errors
        )
        if temporary and preserve_artifacts:
            print("Artifacts retained: {}".format(run_root), flush=True)
        code = _exit_code(passed, failed, inconclusive, errors)
        return code
    except Exception:
        preserve_artifacts = True
        raise
    finally:
        _cleanup_run_root(run_root, temporary, preserve_artifacts)


def run_heavy_mode(mode, iterations, seed, workdir=None):
    if iterations > MAX_HEAVY_ITERATIONS:
        print(
            "HARNESS ERROR: {} is limited to {} iterations to bound disk usage".format(
                mode, MAX_HEAVY_ITERATIONS
            ),
            file=sys.stderr,
        )
        return 2

    started_at = time.monotonic()
    generator = random.Random(seed)
    run_root, temporary = _create_run_root(workdir)
    _print_banner(mode, seed, iterations, run_root, temporary)

    passed = failed = inconclusive = errors = 0
    preserve_artifacts = not temporary
    try:
        for iteration in range(1, iterations + 1):
            iteration_directory = os.path.join(
                run_root, "iteration-{:06d}".format(iteration)
            )
            os.mkdir(iteration_directory)

            if mode == "large-value":
                scenarios = make_large_value_scenarios(generator)
                cases = [
                    (name, requests, len(requests) - 1)
                    for name, requests in scenarios
                ]
            else:
                cases = [(
                    "max-batch",
                    [make_max_batch_request(generator)],
                    0,
                )]

            case_results = []
            iteration_failed = False
            for case_index, (case_name, requests, target_index) in enumerate(cases):
                case_directory = os.path.join(
                    iteration_directory,
                    "case-{:02d}-{}".format(case_index + 1, case_name),
                )
                os.mkdir(case_directory)
                runner = IterationRunner(
                    case_directory,
                    iteration * 10 + case_index,
                    generator,
                )
                delay = generator.uniform(0.0, 0.001)
                infrastructure_error = None
                try:
                    result = runner.run_stress(requests, target_index, delay)
                except Exception as error:
                    infrastructure_error = error
                    result = {
                        "result": "ERROR",
                        "reason": "harness/environment error: {}".format(error),
                        "attempts": runner.attempts,
                        "statuses": dict(runner.statuses),
                        "sent_count": len(runner.sent_ids),
                        "pending_at_kill": None,
                        "protocol_pending_at_kill": None,
                        "kill_delay_ms": round(delay * 1000, 3),
                        "actual": None,
                        "candidates": [],
                    }
                finally:
                    try:
                        runner.close()
                    except Exception as error:
                        if infrastructure_error is None:
                            infrastructure_error = error
                            result = {
                                "result": "ERROR",
                                "reason": "harness/environment cleanup error: {}".format(error),
                                "attempts": runner.attempts,
                                "statuses": dict(runner.statuses),
                                "sent_count": len(runner.sent_ids),
                                "pending_at_kill": None,
                                "protocol_pending_at_kill": None,
                                "kill_delay_ms": round(delay * 1000, 3),
                                "actual": None,
                                "candidates": [],
                            }

                if result["result"] == "PASS":
                    pass
                elif result["result"] == "FAIL":
                    iteration_failed = True
                    preserve_artifacts = True
                else:
                    preserve_artifacts = True

                if result["result"] != "PASS":
                    try:
                        _report_failure(
                            result,
                            seed,
                            iteration,
                            "{}:{}".format(mode, case_name),
                            case_directory,
                            infrastructure_error,
                        )
                    except OSError as error:
                        errors += 1
                        result["reason"] = (
                            "{}; unable to write failure metadata: {}".format(
                                result["reason"], error
                            )
                        )
                result["case"] = case_name
                case_results.append(result)
                if result["result"] in ("FAIL", "ERROR"):
                    break

            if iteration_failed:
                status = "FAIL"
            elif any(result["result"] == "ERROR" for result in case_results):
                status = "ERROR"
            elif any(result["result"] == "INCONCLUSIVE" for result in case_results):
                status = "INCONCLUSIVE"
            else:
                status = "PASS"
            if status == "PASS":
                passed += 1
            elif status == "FAIL":
                failed += 1
            elif status == "ERROR":
                errors += 1
            else:
                inconclusive += 1
            pending_count = sum(
                result.get("pending_at_kill") is True
                for result in case_results
            )
            reasons = "; ".join(
                "{}: {}".format(result["case"], result["reason"])
                for result in case_results
            )
            combined = {
                "result": status,
                "reason": reasons,
                "kill_delay_ms": ",".join(
                    str(result.get("kill_delay_ms", "n/a"))
                    for result in case_results
                ),
                "pending_at_kill": pending_count > 0,
                "statuses": {
                    operation_id: response
                    for result in case_results
                    for operation_id, response in result.get("statuses", {}).items()
                },
                "sent_count": sum(
                    result.get("sent_count", len(result.get("attempts", [])))
                    for result in case_results
                ),
            }
            _print_iteration(iteration, iterations, combined, mode)
            if status in ("FAIL", "ERROR"):
                break

        verdict = (
            "ERROR" if errors else
            "FAIL" if failed else
            "INCONCLUSIVE" if inconclusive else
            "PASS" if passed == iterations else
            "ERROR"
        )
        _print_summary(
            passed, failed, inconclusive, seed, started_at, verdict, errors
        )
        if temporary and preserve_artifacts:
            _safe_print("Artifacts retained: {}".format(run_root), flush=True)
        return _exit_code(passed, failed, inconclusive, errors)
    except Exception:
        preserve_artifacts = True
        raise
    finally:
        _cleanup_run_root(run_root, temporary, preserve_artifacts)


def main(argv=None):
    started_at = time.monotonic()
    args = build_parser().parse_args(argv)
    try:
        check_prerequisites()
    except HarnessError as error:
        _safe_print("HARNESS/ENVIRONMENT ERROR: {}".format(error), file=sys.stderr)
        _print_summary(0, 0, 0, args.seed, started_at, "ERROR", errors=1)
        return 3

    if args.mode not in SUPPORTED_MODES:
        _safe_print("Aid_Map Crash Torture")
        _safe_print("Mode: {}".format(args.mode))
        _safe_print("Seed: {}".format(args.seed))
        _safe_print("Iterations: {}".format(args.iterations))
        _safe_print("Writer count: 0")
        _safe_print("Independent oracle: enabled")
        _safe_print("Database/artifact location: not created")
        _safe_print("Retention: no run started")
        _safe_print(
            "NOT IMPLEMENTED: mode {}".format(args.mode),
            file=sys.stderr,
        )
        _print_summary(0, 0, 0, args.seed, started_at, "NOT_IMPLEMENTED", errors=1)
        return 2
    if args.mode in ("large-value", "max-batch") and args.iterations > MAX_HEAVY_ITERATIONS:
        _safe_print(
            "HARNESS/ARGUMENT ERROR: {} allows at most {} iterations".format(
                args.mode, MAX_HEAVY_ITERATIONS
            ),
            file=sys.stderr,
        )
        _print_summary(
            0, 0, 0, args.seed, started_at, "INVALID_ARGUMENT", errors=1
        )
        return 2
    try:
        if args.mode in ("large-value", "max-batch"):
            return run_heavy_mode(
                args.mode,
                args.iterations,
                args.seed,
                args.workdir,
            )
        return run_basic(args.iterations, args.seed, args.workdir)
    except (OSError, HarnessError, ValueError) as error:
        _safe_print(
            "HARNESS/ENVIRONMENT ERROR: {}".format(error),
            file=sys.stderr,
            flush=True,
        )
        _print_summary(0, 0, 0, args.seed, started_at, "ERROR", errors=1)
        return 3


if __name__ == "__main__":
    sys.exit(main())
