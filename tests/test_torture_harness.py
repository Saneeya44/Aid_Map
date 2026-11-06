import base64
import unittest

import torture


class TortureHarnessTests(unittest.TestCase):
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
        request = {
            "id": "op-1",
            "op": "put",
            "key": base64.b64encode(b"key").decode("ascii"),
            "value": base64.b64encode(b"value").decode("ascii"),
        }
        attempts = [{"sequence": 0, "request": request}]
        responses = {"op-1": ("ACK", "")}

        status, candidates, _ = torture.verify_oracle(
            attempts, responses, {}
        )

        self.assertEqual(status, "FAIL")
        self.assertEqual(candidates, [{b"key": b"value"}])

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
