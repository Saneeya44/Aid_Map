import base64
import contextlib
import io
import json
import os
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

import torture
from src.cli import serve
from src.database import Database


class TortureHarnessTests(unittest.TestCase):
    @staticmethod
    def _put(operation_id, key, value):
        return {
            "id": operation_id,
            "op": "put",
            "key": base64.b64encode(key).decode("ascii"),
            "value": base64.b64encode(value).decode("ascii"),
        }

    @staticmethod
    def _delete(operation_id, key):
        return {
            "id": operation_id,
            "op": "delete",
            "key": base64.b64encode(key).decode("ascii"),
        }

    @staticmethod
    def _attempt(sequence, request, writer=0):
        return {"sequence": sequence, "writer": writer, "request": request}

    def test_parse_ack_and_err_responses(self):
        self.assertEqual(
            torture.parse_response("ACK op-1\n"),
            ("ACK", "op-1", ""),
        )
        self.assertEqual(
            torture.parse_response("ERR op-2 checksum mismatch\n"),
            ("ERR", "op-2", "checksum mismatch"),
        )

    def test_parse_rejects_malformed_protocol_line(self):
        for line in ("OK op-1\n", "ACK\n", "ERR op-1\n", "ACK id extra\n"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    torture.parse_response(line)

    def test_decode_dump_uses_cli_base64_format(self):
        key = b"\x00binary"
        value = b"\xffdata"
        output = "key={} value={}\n".format(
            base64.b64encode(key).decode("ascii"),
            base64.b64encode(value).decode("ascii"),
        )
        self.assertEqual(torture.decode_dump(output), {key: value})
        self.assertEqual(torture.decode_dump(""), {})

    def test_verifier_accepts_unacknowledged_request_as_optional_commit(self):
        request = {
            "id": "op-1",
            "op": "put",
            "key": base64.b64encode(b"key").decode("ascii"),
            "value": base64.b64encode(b"value").decode("ascii"),
        }
        attempts = [{"sequence": 0, "request": request}]

        status, candidates, _ = torture.verify_oracle(
            attempts, {}, {b"key": b"value"}
        )

        self.assertEqual(status, "PASS")
        self.assertIn({}, candidates)
        self.assertIn({b"key": b"value"}, candidates)

    def test_verifier_fails_when_acknowledged_write_is_missing(self):
        request = self._put("op-1", b"key", b"value")
        attempts = [{"sequence": 0, "request": request}]
        responses = {"op-1": ("ACK", "")}

        status, candidates, _ = torture.verify_oracle(
            attempts, responses, {}
        )

        self.assertEqual(status, "FAIL")
        self.assertEqual(candidates, [{b"key": b"value"}])

    def test_verifier_rejects_wrong_value_for_acknowledged_put(self):
        request = self._put("op-1", b"key", b"committed")
        status, candidates, _ = torture.verify_oracle(
            [self._attempt(0, request)],
            {"op-1": ("ACK", "")},
            {b"key": b"invented"},
        )
        self.assertEqual(status, "FAIL")
        self.assertEqual(candidates, [{b"key": b"committed"}])

    def test_verifier_rejects_half_applied_acknowledged_batch(self):
        request = {
            "id": "batch-1",
            "op": "batch",
            "operations": [
                {
                    "op": "put",
                    "key": base64.b64encode(b"first").decode("ascii"),
                    "value": base64.b64encode(b"one").decode("ascii"),
                },
                {
                    "op": "put",
                    "key": base64.b64encode(b"second").decode("ascii"),
                    "value": base64.b64encode(b"two").decode("ascii"),
                },
            ],
        }
        status, candidates, _ = torture.verify_oracle(
            [self._attempt(0, request)],
            {"batch-1": ("ACK", "")},
            {b"first": b"one"},
        )
        self.assertEqual(status, "FAIL")
        self.assertEqual(candidates, [{b"first": b"one", b"second": b"two"}])

    def test_verifier_rejects_invented_key_and_value(self):
        request = self._put("op-1", b"key", b"value")
        status, _, _ = torture.verify_oracle(
            [self._attempt(0, request)],
            {"op-1": ("ACK", "")},
            {b"key": b"value", b"extra": b"invented"},
        )
        self.assertEqual(status, "FAIL")

    def test_verifier_rejects_lost_acknowledged_delete(self):
        attempts = [
            self._attempt(0, self._put("put-1", b"key", b"value")),
            self._attempt(1, self._delete("delete-1", b"key"), writer=1),
        ]
        status, candidates, _ = torture.verify_oracle(
            attempts,
            {"put-1": ("ACK", ""), "delete-1": ("ACK", "")},
            {b"key": b"value"},
        )
        self.assertEqual(status, "FAIL")
        self.assertEqual(candidates, [{}])

    def test_verifier_rejects_impossible_order_of_overlapping_writes(self):
        attempts = [
            self._attempt(0, self._put("writer-a", b"shared", b"A"), writer=0),
            self._attempt(1, self._put("writer-b", b"shared", b"B"), writer=1),
        ]
        status, candidates, _ = torture.verify_oracle(
            attempts,
            {"writer-a": ("ACK", ""), "writer-b": ("ACK", "")},
            {b"shared": b"A"},
        )
        self.assertEqual(status, "FAIL")
        self.assertEqual(candidates, [{b"shared": b"B"}])

    def test_verifier_accepts_each_valid_serial_order_for_overlapping_writes(self):
        for first_id, first_value, second_id, second_value in (
            ("writer-a", b"A", "writer-b", b"B"),
            ("writer-b", b"B", "writer-a", b"A"),
        ):
            with self.subTest(first_id=first_id):
                attempts = [
                    self._attempt(
                        0,
                        self._put(first_id, b"shared", first_value),
                        writer=0,
                    ),
                    self._attempt(
                        1,
                        self._put(second_id, b"shared", second_value),
                        writer=1,
                    ),
                ]
                status, candidates, _ = torture.verify_oracle(
                    attempts,
                    {
                        first_id: ("ACK", ""),
                        second_id: ("ACK", ""),
                    },
                    {b"shared": second_value},
                )
                self.assertEqual(status, "PASS")
                self.assertEqual(candidates, [{b"shared": second_value}])

    def test_verifier_accepts_unacknowledged_request_not_committed(self):
        attempts = [self._attempt(0, self._put("op-1", b"key", b"value"))]
        status, candidates, _ = torture.verify_oracle(attempts, {}, {})
        self.assertEqual(status, "PASS")
        self.assertIn({}, candidates)

    def test_verifier_accepts_unacknowledged_request_committed(self):
        attempts = [self._attempt(0, self._put("op-1", b"key", b"value"))]
        status, candidates, _ = torture.verify_oracle(
            attempts, {}, {b"key": b"value"}
        )
        self.assertEqual(status, "PASS")
        self.assertIn({b"key": b"value"}, candidates)

    def test_verifier_uses_fsynced_ack_events_from_oracle_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "oracle.jsonl")
            oracle = torture.OracleLog(path)
            request = self._put("op-1", b"key", b"value")
            oracle.record({
                "event": "request",
                "sequence": 0,
                "request": request,
            })
            oracle.record({"event": "sent", "sequence": 0, "id": "op-1"})
            oracle.record({
                "event": "response",
                "id": "op-1",
                "response": "ACK",
                "reason": "",
            })
            oracle.close()

            status, candidates, _ = torture.verify_oracle_log(
                path, {b"key": b"value"}
            )

        self.assertEqual(status, "PASS")
        self.assertEqual(candidates, [{b"key": b"value"}])

    def test_verifier_returns_inconclusive_for_unconfirmed_request_send(self):
        events = [{
            "event": "request",
            "sequence": 0,
            "id": "op-1",
            "request": self._put("op-1", b"key", b"value"),
        }]
        status, candidates, _ = torture.verify_oracle_events(events, {})
        self.assertEqual(status, "INCONCLUSIVE")
        self.assertEqual(candidates, [])

    def test_verifier_returns_inconclusive_after_pipe_send_error(self):
        events = [{
            "event": "send_error",
            "id": "op-1",
            "reason": "pipe closed",
        }]
        status, candidates, _ = torture.verify_oracle_events(events, {})
        self.assertEqual(status, "INCONCLUSIVE")
        self.assertEqual(candidates, [])

    def test_verifier_does_not_treat_unknown_batch_item_as_delete(self):
        request = {
            "id": "batch-1",
            "op": "batch",
            "operations": [{
                "op": "invalid",
                "key": base64.b64encode(b"key").decode("ascii"),
            }],
        }
        status, candidates, _ = torture.verify_oracle(
            [self._attempt(0, request)],
            {"batch-1": ("ACK", "")},
            {},
        )
        self.assertEqual(status, "INCONCLUSIVE")
        self.assertEqual(candidates, [])

    def test_large_value_mode_generates_exact_limit_overwrite_and_delete(self):
        scenarios = torture.make_large_value_scenarios(
            random.Random(41)
        )
        self.assertEqual(
            [name for name, _ in scenarios],
            ["large-put", "large-overwrite", "large-delete"],
        )

        put_request = scenarios[0][1][0]
        self.assertEqual(put_request["op"], "put")
        self.assertEqual(
            len(base64.b64decode(put_request["value"])),
            1024 * 1024,
        )

        overwrite_requests = scenarios[1][1]
        self.assertEqual(
            [request["op"] for request in overwrite_requests],
            ["put", "put"],
        )
        self.assertEqual(
            len(base64.b64decode(overwrite_requests[1]["value"])),
            1024 * 1024,
        )
        self.assertNotEqual(
            overwrite_requests[0]["value"],
            overwrite_requests[1]["value"],
        )

        delete_requests = scenarios[2][1]
        self.assertEqual(
            [request["op"] for request in delete_requests],
            ["put", "delete"],
        )
        self.assertEqual(
            len(base64.b64decode(delete_requests[0]["value"])),
            1024 * 1024,
        )

    def test_max_batch_mode_generates_exactly_10000_mixed_repeated_key_operations(self):
        request = torture.make_max_batch_request(random.Random(17))
        operations = request["operations"]
        self.assertEqual(request["op"], "batch")
        self.assertEqual(len(operations), 10000)
        self.assertEqual(operations[0]["op"], "put")
        self.assertEqual(operations[1]["op"], "delete")
        self.assertEqual(
            len({operation["key"] for operation in operations}),
            128,
        )
        self.assertTrue(any(operation["op"] == "put" for operation in operations))
        self.assertTrue(any(operation["op"] == "delete" for operation in operations))

    def test_10001_operation_serve_batch_is_rejected_without_database_change(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "oversize.kv")
            with Database(database_path) as database:
                database.put(b"keep", b"unchanged")

            with open(database_path, "rb") as file:
                before = file.read()

            request = {
                "id": "too-many",
                "op": "batch",
                "operations": [
                    {
                        "op": "delete",
                        "key": base64.b64encode(b"repeat").decode("ascii"),
                    }
                    for _ in range(10001)
                ],
            }
            output = io.StringIO()
            error_output = io.StringIO()
            exit_code = serve(
                database_path,
                input_stream=io.StringIO(json.dumps(request) + "\n"),
                output_stream=output,
                error_stream=error_output,
            )

            with open(database_path, "rb") as file:
                after = file.read()

        self.assertEqual(exit_code, 0)
        self.assertTrue(output.getvalue().startswith("ERR too-many "))
        self.assertEqual(error_output.getvalue(), "")
        self.assertEqual(after, before)

    def test_verifier_returns_inconclusive_for_nonprefix_ack_history(self):
        attempts = [
            {
                "sequence": index,
                "request": {
                    "id": "op-{}".format(index),
                    "op": "delete",
                    "key": base64.b64encode(b"key").decode("ascii"),
                },
            }
            for index in range(2)
        ]
        responses = {"op-1": ("ACK", "")}

        status, candidates, _ = torture.verify_oracle(
            attempts, responses, {}
        )

        self.assertEqual(status, "INCONCLUSIVE")
        self.assertEqual(candidates, [])

    def test_cli_arguments_and_implemented_modes(self):
        args = torture.build_parser().parse_args(["--seed", "123"])
        self.assertEqual(args.iterations, 500)
        self.assertEqual(args.seed, 123)
        self.assertEqual(args.mode, "basic")
        self.assertIsNone(args.workdir)

        for mode in ("recovery-crash", "compaction-crash", "disk-full"):
            with self.subTest(mode=mode):
                args = torture.build_parser().parse_args([
                    "--seed", "1", "--mode", mode
                ])
                self.assertIn(args.mode, torture.SUPPORTED_MODES)
        with self.assertRaises(SystemExit):
            torture.build_parser().parse_args([])

    def test_cli_rejects_unknown_mode_with_nonzero_argument_exit(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:
                torture.build_parser().parse_args([
                    "--seed", "123", "--mode", "unknown"
                ])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", output.getvalue())

    def test_exit_codes_only_pass_when_all_iterations_pass(self):
        self.assertEqual(torture._exit_code(5, 0, 0), 0)
        self.assertEqual(torture._exit_code(4, 1, 0), 1)
        self.assertEqual(torture._exit_code(4, 0, 1), 2)
        self.assertEqual(torture._exit_code(4, 0, 0, errors=1), 3)
        self.assertEqual(torture._exit_code(0, 0, 0), 2)

    def test_summary_format_includes_counts_seed_elapsed_and_verdict(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with patch.object(torture.time, "monotonic", return_value=15.0):
                torture._print_summary(
                    5, 0, 0, 12345, 10.0, "PASS", errors=0
                )
        self.assertIn(
            "SUMMARY PASS=5 FAIL=0 INCONCLUSIVE=0 ERROR=0 seed=12345 "
            "elapsed=5.00s verdict=PASS",
            output.getvalue(),
        )

    def test_startup_banner_reports_oracle_writer_count_and_retention(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            torture._print_banner("basic", 123, 5, r"C:\Temp\aid-map-run", True)
        text = output.getvalue()
        self.assertIn("Aid_Map Crash Torture", text)
        self.assertIn("Mode: basic", text)
        self.assertIn("Seed: 123", text)
        self.assertIn("Iterations: 5", text)
        self.assertIn("Writer count: 2", text)
        self.assertIn("Independent oracle: enabled", text)
        self.assertIn("C:\\Temp\\aid-map-run", text)
        self.assertIn("removed after complete PASS", text)

    def test_mode_guard_reports_unavailable_mode_without_running_it(self):
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(torture, "SUPPORTED_MODES", ("basic",)):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                exit_code = torture.main([
                    "--seed", "123", "--mode", "recovery-crash"
                ])
        self.assertEqual(exit_code, 2)
        self.assertIn("NOT IMPLEMENTED: mode recovery-crash", errors.getvalue())
        self.assertIn("verdict=NOT_IMPLEMENTED", output.getvalue())

    def test_recovery_crash_mode_kills_during_replay_and_verifies_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = torture.run_recovery_crash(1, 7731, directory)

            self.assertEqual(exit_code, 0, output.getvalue())
            text = output.getvalue()
            self.assertIn("ITER 1/1 PASS", text)
            self.assertIn("replay kill confirmed", text)
            self.assertIn("open/check/dump passed", text)
            self.assertIn("all ACKs and atomic batch match oracle", text)
            self.assertIn("SUMMARY PASS=1 FAIL=0 INCONCLUSIVE=0", text)

    def test_recovery_crash_without_replayed_put_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "empty.kv")
            with open(database_path, "wb"):
                pass

            confirmed, _, reason = torture._kill_during_recovery_replay(
                database_path, 1
            )

        self.assertFalse(confirmed)
        self.assertIn("before the replay pause", reason)

    def test_recovery_crash_without_pause_cannot_report_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(
                torture,
                "_kill_during_recovery_replay",
                return_value=(False, None, "replay pause was not observed"),
            ):
                with contextlib.redirect_stdout(output):
                    exit_code = torture.run_recovery_crash(1, 7731, directory)

        self.assertEqual(exit_code, 2)
        self.assertIn("ITER 1/1 INCONCLUSIVE", output.getvalue())
        self.assertIn("verdict=INCONCLUSIVE", output.getvalue())
        self.assertNotIn("ITER 1/1 PASS", output.getvalue())

    def test_missing_prerequisites_are_reported_without_starting_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                torture.HarnessError, "required project files are missing"
            ):
                torture.check_prerequisites(
                    root=directory,
                    python_version=(3, 15, 0),
                    executable=sys.executable,
                )

    def test_main_returns_environment_error_code_for_missing_prerequisite(self):
        output = io.StringIO()
        with patch.object(
            torture,
            "check_prerequisites",
            side_effect=torture.HarnessError("src/cli.py is missing"),
        ):
            with contextlib.redirect_stderr(output), contextlib.redirect_stdout(
                io.StringIO()
            ):
                exit_code = torture.main(["--seed", "123"])
        self.assertEqual(exit_code, 3)
        self.assertIn("HARNESS/ENVIRONMENT ERROR", output.getvalue())

    def test_prerequisite_check_accepts_python_37_and_required_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source_directory = os.path.join(directory, "src")
            os.mkdir(source_directory)
            for name in ("cli.py", "database.py", "storage.py", "locking.py"):
                with open(os.path.join(source_directory, name), "w") as file:
                    file.write("")
            torture.check_prerequisites(
                root=directory,
                python_version=(3, 7, 0),
                executable=sys.executable,
            )

    def test_failure_artifacts_are_retained_with_reproduction_metadata(self):
        with tempfile.TemporaryDirectory() as parent:
            run_root, temporary = torture._create_run_root(parent)
            try:
                iteration_directory = os.path.join(run_root, "iteration-000001")
                os.mkdir(iteration_directory)
                with open(os.path.join(iteration_directory, "database.kv"), "wb") as file:
                    file.write(b"fixture")
                with open(os.path.join(iteration_directory, "oracle.jsonl"), "w") as file:
                    file.write("{}\n")

                result = {
                    "result": "FAIL",
                    "reason": "acknowledged value missing",
                    "attempts": [],
                    "candidates": [],
                    "actual": {},
                }
                metadata = torture._write_failure_metadata(
                    iteration_directory,
                    torture._failure_metadata(12345, "basic", 1, result),
                )
                torture._cleanup_run_root(run_root, temporary, True)

                self.assertTrue(os.path.isfile(metadata))
                self.assertTrue(os.path.isfile(
                    os.path.join(iteration_directory, "database.kv")
                ))
                self.assertTrue(os.path.isfile(
                    os.path.join(iteration_directory, "oracle.jsonl")
                ))
                with open(metadata, encoding="ascii") as file:
                    saved = json.load(file)
                self.assertEqual(saved["seed"], 12345)
                self.assertEqual(saved["iteration"], 1)
                self.assertEqual(saved["result"], "FAIL")
            finally:
                if os.path.isdir(run_root):
                    import shutil
                    shutil.rmtree(run_root)


if __name__ == "__main__":
    unittest.main()
