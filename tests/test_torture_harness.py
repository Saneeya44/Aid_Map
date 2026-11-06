import base64
import os
import tempfile
import unittest

import torture


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

        status, candidates, explanation = torture.verify_oracle(
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

    def test_cli_arguments_and_unimplemented_modes(self):
        args = torture.build_parser().parse_args(["--seed", "123"])
        self.assertEqual(args.iterations, 500)
        self.assertEqual(args.seed, 123)
        self.assertEqual(args.mode, "basic")
        self.assertIsNone(args.workdir)

        unsupported = torture.build_parser().parse_args([
            "--seed", "1", "--mode", "compaction-crash"
        ])
        self.assertEqual(unsupported.mode, "compaction-crash")
        with self.assertRaises(SystemExit):
            torture.build_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
