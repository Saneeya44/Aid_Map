# Aid_Map

## Database CLI

The CLI stores data in a persistent database at `%LOCALAPPDATA%\Aid_Map\aid_map.kv`
on Windows (`~/.local/share/Aid_Map/aid_map.kv` by default on other platforms).
The same database is used when the path is omitted, so data remains available
between commands.

Run commands from the repository root:

```powershell
python -m src.cli put name Anu
python -m src.cli get name
python -m src.cli del name
```

Use `--db PATH` to select another database. Existing path-first commands are also
supported, for example `python -m src.cli put custom.kv name Anu`.

### SERVE protocol

`python -m src.cli serve PATH` (or `python -m src.cli serve --db PATH`) reads
one JSON object per line from standard input until EOF. If no path is supplied,
the normal default database path is used.

Requests use strict Base64 strings for byte keys and values:

```json
{"id":"op-1","op":"put","key":"bmFtZQ==","value":"QW5hbnlh"}
{"id":"op-2","op":"delete","key":"bmFtZQ=="}
{"id":"op-3","op":"batch","operations":[{"op":"put","key":"YQ==","value":"MQ=="},{"op":"delete","key":"Yg=="}]}
```

Keys must decode to 1–1024 bytes; values may be 0–1 MiB. A batch must contain
1–10,000 valid PUT or DELETE operations and is validated completely before it
is submitted to the database. Operation IDs must be nonempty printable ASCII without whitespace; `-` is
reserved for an error response whose request had no usable ID. Base64 must be
canonical RFC 4648 encoding.

Successful requests produce one `ACK <id>` line. Invalid requests or failed
operations produce `ERR <id> <reason>`; when a request has no usable ID, the
response uses `ERR - <reason>`. Reasons are single-line. Deleting a key that is
already absent is acknowledged because the requested end state (key absent)
already holds. Responses are flushed immediately; diagnostics go to stderr.

The database engine writes records through `append_record()`, which calls
`os.fsync()` before the successful PUT, DELETE, or BATCH method returns. SERVE
therefore acknowledges only after those methods return. This describes the
current implementation and is not, by itself, a claim of crash-safety across
every filesystem or hardware failure mode.

## V1 torture harness

Run the basic seeded crash test from the repository root:

```powershell
python torture.py --iterations 5 --seed 12345
```

`--iterations` defaults to 500; `--seed` is required. The basic mode starts a
SERVE subprocess per iteration, sends concurrent random PUT, DELETE, and BATCH
requests, force-kills the process, then checks the recovered database with
separate `open`, `check`, and `dump` CLI subprocesses. `--workdir PATH` retains
artifacts in a newly created unique run directory under PATH; without it, a
temporary directory is used. Request generation and kill delays are seeded,
but OS scheduling means crash timing is not deterministic.

The verifier requires all ACKed writes and allows the single next in-flight
request to have committed or not. It reports INCONCLUSIVE rather than assuming
an outcome when response/history evidence cannot establish a safe serialized
order. `large-value`, `max-batch`, `recovery-crash`, `compaction-crash`, and
`disk-full` modes are currently NOT IMPLEMENTED.
