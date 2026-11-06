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
SUPPORTED_MODES = ("basic", "compaction-crash", "disk-full")
KNOWN_MODES = (
    "basic",
    "large-value",
    "max-batch",
    "recovery-crash",
    "compaction-crash",
    "disk-full",
)
PROCESS_TIMEOUT = 15
READER_TIMEOUT = 10


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
        description="Crash-consistency harness for the Aid_Map CLI"
    )
    parser.add_argument(
        "--iterations",
        type=positive_integer,
        default=500,
        help="number of isolated crash iterations (default: 500)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        required=True,
        help="seed for deterministic request generation and kill delays",
    )
    parser.add_argument(
        "--mode",
        choices=KNOWN_MODES,
        default="basic",
        help="scenario mode (default: basic)",
    )
    parser.add_argument(
        "--workdir",
        help="parent directory for a new unique run directory (default: temporary)",
    )
    return parser


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


def apply_request(state, request):
    operation = request["op"]
    if operation == "put":
        state[base64.b64decode(request["key"])] = base64.b64decode(
            request["value"]
        )
    elif operation == "delete":
        state.pop(base64.b64decode(request["key"]), None)
    elif operation == "batch":
        for item in request["operations"]:
            if item["op"] == "put":
                state[base64.b64decode(item["key"])] = base64.b64decode(
                    item["value"]
                )
            else:
                state.pop(base64.b64decode(item["key"]), None)
    else:
        raise ValueError("Unsupported oracle operation: {}".format(operation))


def verify_oracle(attempts, responses, actual_state):
    """Return (PASS|FAIL|INCONCLUSIVE, candidate states, explanation)."""
    id_to_index = {
        event["request"]["id"]: index for index, event in enumerate(attempts)
    }

    for operation_id, (response_type, reason) in responses.items():
        if operation_id not in id_to_index:
            return "INCONCLUSIVE", [], "response for unknown operation id {}".format(
                operation_id
            )
        if response_type == "ERR":
            return "INCONCLUSIVE", [], "database returned ERR: {}".format(reason)

    acknowledged = sorted(
        id_to_index[operation_id]
        for operation_id, (response_type, _) in responses.items()
        if response_type == "ACK"
    )
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
    for prefix_length in sorted(prefix_lengths):
        state = {}
        for event in attempts[:prefix_length]:
            apply_request(state, event["request"])
        if state not in candidates:
            candidates.append(state)

    if actual_state in candidates:
        return "PASS", candidates, "recovered state matches an allowed serialized prefix"
    return "FAIL", candidates, "recovered state matches no allowed serialized prefix"


class OracleLog:
    def __init__(self, path):
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


class IterationRunner:
    def __init__(self, directory, iteration, generator):
        self.directory = Path(directory)
        self.iteration = iteration
        self.database_path = self.directory / "database.kv"
        self.oracle = OracleLog(str(self.directory / "oracle.jsonl"))
        self.generator = generator
        self.status_lock = threading.Lock()
        self.statuses = {}
        self.reader_errors = []
        self.stderr_lines = []
        self.sent_condition = threading.Condition()
        self.sent_count = 0
        self.attempts = []
        self.attempt_lock = threading.Lock()
        self.sequence_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active_lock = threading.Lock()
        self.active_writers = 0
        self.first_sent = threading.Event()
        self.writer_errors = []
        self.process = None
        self.writers = []
        self.closed = False

    def _close_process_stream(self, stream):
        if stream is None or stream.closed:
            return
        try:
            stream.close()
        except OSError:
            if self.process.poll() is None:
                raise

    def _record_response(self, line):
        try:
            response_type, operation_id, reason = parse_response(line)
        except ValueError as error:
            self.reader_errors.append("{}: {!r}".format(error, line))
            return

        self.oracle.record({
            "event": "response",
            "id": operation_id,
            "response": response_type,
            "reason": reason,
        })
        with self.status_lock:
            if operation_id in self.statuses:
                self.reader_errors.append(
                    "duplicate response for {}".format(operation_id)
                )
            else:
                self.statuses[operation_id] = (response_type, reason)

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
                        self.stop_event.set()
                        break

                    self.oracle.record({
                        "event": "sent",
                        "id": request["id"],
                        "sequence": sequence,
                    })
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
        self._close_process_stream(self.process.stdin)
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
        self.oracle.record({
            "event": "kill",
            "delay_ms": kill_delay_ms,
            "writers_active": active_at_kill,
        })
        self._stop_and_reap()

        for writer in writers:
            writer.join(timeout=READER_TIMEOUT)
            if writer.is_alive():
                raise HarnessError("Writer thread did not stop after force-kill")

        if self.reader_errors:
            return {
                "result": "FAIL",
                "reason": "; ".join(self.reader_errors),
                "kill_delay_ms": kill_delay_ms,
                "active_at_kill": active_at_kill,
            }

        actual_state = read_recovered_dump(self.database_path)
        with self.status_lock:
            statuses = dict(self.statuses)
        result, candidates, explanation = verify_oracle(
            self.attempts,
            statuses,
            actual_state,
        )
        if self.writer_errors and result != "FAIL":
            result = "INCONCLUSIVE"
            explanation = "writer pipe outcome uncertain: {}".format(
                "; ".join(self.writer_errors)
            )
        if not active_at_kill and result != "FAIL":
            result = "INCONCLUSIVE"
            explanation = "force-kill occurred after writers were no longer active"

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
                for thread_name in ("stdout_thread", "stderr_thread"):
                    thread = getattr(self, thread_name, None)
                    if thread is not None:
                        thread.join(timeout=READER_TIMEOUT)
                        if thread.is_alive():
                            raise HarnessError(
                                "Output reader did not stop during cleanup"
                            )
                for stream_name in ("stdin", "stdout", "stderr"):
                    stream = getattr(self.process, stream_name, None)
                    self._close_process_stream(stream)
        finally:
            self.oracle.close()
            self.closed = True


def _format_state(state):
    return {
        base64.b64encode(key).decode("ascii"):
        base64.b64encode(value).decode("ascii")
        for key, value in sorted(state.items())
    }


def _iteration_requests(result):
    return [
        event["request"]["id"]
        for event in result.get("attempts", [])
    ]


def run_basic(iterations, seed, workdir=None):
    generator = random.Random(seed)
    if workdir is None:
        temporary = tempfile.TemporaryDirectory(prefix="aid-map-torture-")
        run_root = temporary.name
    else:
        parent = os.path.abspath(workdir)
        os.makedirs(parent, exist_ok=True)
        run_root = tempfile.mkdtemp(prefix="aid-map-torture-", dir=parent)
        temporary = None

    passed = failed = inconclusive = 0
    first_failure = None
    try:
        for iteration in range(iterations):
            iteration_directory = os.path.join(
                run_root, "iteration-{:06d}".format(iteration + 1)
            )
            os.mkdir(iteration_directory)
            runner = IterationRunner(iteration_directory, iteration + 1, generator)
            try:
                result = runner.run()
            except Exception as error:
                failed += 1
                first_failure = {
                    "iteration": iteration + 1,
                    "reason": str(error),
                    "attempts": runner.attempts,
                    "actual": None,
                    "candidates": [],
                }
                print(
                    "ITER {}/{} FAIL {}".format(
                        iteration + 1, iterations, error
                    ),
                    flush=True,
                )
                break
            finally:
                runner.close()

            if result["result"] == "PASS":
                passed += 1
            elif result["result"] == "FAIL":
                failed += 1
                first_failure = result
            else:
                inconclusive += 1

            print(
                "ITER {}/{} {} delay={}ms active={} acked={}/{} {}".format(
                    iteration + 1,
                    iterations,
                    result["result"],
                    result["kill_delay_ms"],
                    result["active_at_kill"],
                    sum(status[0] == "ACK" for status in result.get("statuses", {}).values()),
                    len(result.get("attempts", [])),
                    result["reason"],
                ),
                flush=True,
            )
            if result["result"] == "FAIL":
                break

        if first_failure:
            attempted_ids = _iteration_requests(first_failure)
            print("FIRST FAILURE seed={} iteration={}".format(
                seed, first_failure["iteration"]
            ))
            print("Operation IDs: {}".format(", ".join(attempted_ids) or "(none)"))
            print("Reason: {}".format(first_failure["reason"]))
            print("Expected possibilities: {}".format(
                json.dumps(
                    [_format_state(state) for state in first_failure.get("candidates", [])],
                    sort_keys=True,
                )
            ))
            actual = first_failure.get("actual")
            print("Actual recovered state: {}".format(
                json.dumps(_format_state(actual), sort_keys=True)
                if isinstance(actual, dict)
                else "(unavailable)"
            ))

        print(
            "SUMMARY PASS={} FAIL={} INCONCLUSIVE={} seed={} iterations={}".format(
                passed, failed, inconclusive, seed, iterations
            ),
            flush=True,
        )
        if temporary is None:
            print("Artifacts: {}".format(run_root), flush=True)

        if failed:
            return 1
        if inconclusive:
            return 2
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


def _scenario_root(workdir):
    if workdir is None:
        temporary = tempfile.TemporaryDirectory(prefix="aid-map-torture-")
        return temporary, temporary.name
    parent = os.path.abspath(workdir)
    os.makedirs(parent, exist_ok=True)
    return None, tempfile.mkdtemp(prefix="aid-map-torture-", dir=parent)


def _run_python(code, *arguments, input_text=None):
    try:
        result = subprocess.run(
            [sys.executable, "-u", "-c", code, *map(str, arguments)],
            cwd=str(ROOT),
            env=_environment(),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise HarnessError("Injected scenario process timed out") from error
    if result.returncode != 0:
        raise HarnessError(
            "Injected scenario process failed: {}".format(
                (result.stderr or result.stdout).strip()
            )
        )
    return result


def _prepare_compaction_database(database_path, generator):
    values = {}
    with open(database_path, "wb"):
        pass
    from src.database import Database

    with Database(database_path) as database:
        for index in range(24):
            key = "key-{:02d}".format(index).encode("ascii")
            for version in range(generator.randint(3, 7)):
                value = random_value(generator)
                database.put(key, value)
            if index % 3 == 0:
                database.delete(key)
            else:
                values[key] = value
    return values


def _kill_compaction_at_replace(database_path, phase):
    code = r"""
import sys
import src.storage as storage
from src.database import Database

original_replace = storage.os.replace
original_encode_record = storage.encode_record
phase = sys.argv[2]

def pause():
    print("READY", flush=True)
    sys.stdin.readline()

def pause_during_write(record_type, payload):
    record = original_encode_record(record_type, payload)
    pause()
    return record

def pause_at_replace(source, destination):
    if phase == "after":
        original_replace(source, destination)
    pause()
    if phase == "before":
        original_replace(source, destination)
    raise SystemExit(17)

if phase == "write":
    storage.encode_record = pause_during_write
else:
    storage.os.replace = pause_at_replace
with Database(sys.argv[1]) as database:
    database.compact()
"""
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                code,
                str(database_path),
                phase,
            ],
            cwd=str(ROOT),
            env=_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = process.stdout.readline()
        if ready != "READY\n":
            stderr = process.stderr.read()
            raise HarnessError(
                "Compaction did not reach replacement boundary: {}".format(
                    stderr.strip()
                )
            )
        process.kill()
        process.wait(timeout=PROCESS_TIMEOUT)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
    except BaseException:
        if "process" in locals() and process.poll() is None:
            process.kill()
            process.wait(timeout=PROCESS_TIMEOUT)
        raise


def _run_compaction_crash(iterations, seed, workdir):
    generator = random.Random(seed)
    temporary, run_root = _scenario_root(workdir)
    passed = failed = 0
    try:
        for iteration in range(1, iterations + 1):
            iteration_directory = os.path.join(
                run_root, "iteration-{:06d}".format(iteration)
            )
            os.mkdir(iteration_directory)
            database_path = os.path.join(iteration_directory, "database.kv")
            expected = _prepare_compaction_database(database_path, generator)
            original_size = os.path.getsize(database_path)
            phase = generator.choice(("write", "before", "after"))

            try:
                _kill_compaction_at_replace(database_path, phase)
                actual = read_recovered_dump(database_path)
                if actual != expected:
                    raise HarnessError("recovered state differs from live-key oracle")
                compacted_size = os.path.getsize(database_path)
                if phase == "after" and compacted_size >= original_size:
                    raise HarnessError("compaction did not reclaim database space")
                if phase != "after" and compacted_size != original_size:
                    raise HarnessError("pre-replacement kill changed the original log")
                passed += 1
                result = "PASS"
                reason = "recovered all live keys after {}".format({
                    "write": "interrupted snapshot write",
                    "before": "pre-replacement kill",
                    "after": "replacement",
                }[phase])
            except Exception as error:
                failed += 1
                result = "FAIL"
                reason = str(error)
            print(
                "ITER {}/{} {} phase={} {}".format(
                    iteration,
                    iterations,
                    result,
                    {
                        "write": "during-snapshot-write",
                        "before": "before-replace",
                        "after": "after-replace",
                    }[phase],
                    reason,
                ),
                flush=True,
            )
            if result == "FAIL":
                print("FIRST FAILURE seed={} iteration={}".format(seed, iteration))
                break

        print(
            "SUMMARY PASS={} FAIL=0 seed={} iterations={}".format(
                passed, seed, iterations
            ) if failed == 0 else
            "SUMMARY PASS={} FAIL={} seed={} iterations={}".format(
                passed, failed, seed, iteration
            ),
            flush=True,
        )
        if temporary is None:
            print("Artifacts: {}".format(run_root), flush=True)
        return 1 if failed else 0
    finally:
        if temporary is not None:
            temporary.cleanup()


def _run_disk_full(iterations, seed, workdir):
    generator = random.Random(seed)
    temporary, run_root = _scenario_root(workdir)
    passed = failed = 0
    code = r"""
import errno
import sys
import src.storage as storage
from src.cli import main

original_write = storage.os.write
failed = False

def partial_then_enospc(fd, data):
    global failed
    if not failed:
        failed = True
        data = bytes(data)
        original_write(fd, data[:max(1, len(data) // 2)])
        raise OSError(errno.ENOSPC, "simulated disk full")
    return original_write(fd, data)

storage.os.write = partial_then_enospc
raise SystemExit(main(["serve", sys.argv[1]]))
"""
    try:
        for iteration in range(1, iterations + 1):
            iteration_directory = os.path.join(
                run_root, "iteration-{:06d}".format(iteration)
            )
            os.mkdir(iteration_directory)
            database_path = os.path.join(iteration_directory, "database.kv")
            from src.database import Database

            key = ("committed-{}".format(generator.getrandbits(32))).encode("ascii")
            value = random_value(generator)
            with Database(database_path) as database:
                database.put(key, value)

            request_id = "disk-full-{}".format(iteration)
            request = {
                "id": request_id,
                "op": "put",
                "key": _b64(b"uncommitted"),
                "value": _b64(b"new-value"),
            }
            try:
                result = _run_python(
                    code,
                    database_path,
                    input_text=json.dumps(request) + "\n",
                )
                expected_error = "ERR {} [Errno 28] simulated disk full\n".format(
                    request_id
                )
                if result.stdout != expected_error:
                    raise HarnessError(
                        "expected ERR without ACK, got {!r}".format(result.stdout)
                    )
                actual = read_recovered_dump(database_path)
                if actual != {key: value}:
                    raise HarnessError("partial failed write changed committed state")
                passed += 1
                reason = "partial ENOSPC produced ERR and preserved old state"
                result_label = "PASS"
            except Exception as error:
                failed += 1
                reason = str(error)
                result_label = "FAIL"
            print(
                "ITER {}/{} {} {}".format(
                    iteration, iterations, result_label, reason
                ),
                flush=True,
            )
            if failed:
                print("FIRST FAILURE seed={} iteration={}".format(seed, iteration))
                break

        print(
            "SUMMARY PASS={} FAIL={} seed={} iterations={}".format(
                passed, failed, seed, iteration if iterations else 0
            ),
            flush=True,
        )
        if temporary is None:
            print("Artifacts: {}".format(run_root), flush=True)
        return 1 if failed else 0
    finally:
        if temporary is not None:
            temporary.cleanup()


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.mode not in SUPPORTED_MODES:
        print("NOT IMPLEMENTED: mode {}".format(args.mode))
        print(
            "SUMMARY PASS=0 FAIL=0 INCONCLUSIVE=0 NOT_IMPLEMENTED=1 seed={} "
            "iterations={}".format(args.seed, args.iterations)
        )
        return 2
    if args.mode == "basic":
        return run_basic(args.iterations, args.seed, args.workdir)
    if args.mode == "compaction-crash":
        return _run_compaction_crash(args.iterations, args.seed, args.workdir)
    return _run_disk_full(args.iterations, args.seed, args.workdir)


if __name__ == "__main__":
    sys.exit(main())
