# Aid_Map

## Quick Judge Start

Requirements: Python 3.7 or newer and the Aid_Map source files in this
repository. The harness uses only Python's standard library; no third-party
Python packages are required. The current workspace was verified with Python
3.15.0.

From the repository root, run the documented 500-iteration basic test:

```powershell
python torture.py --iterations 500 --seed 12345 --mode basic
```

For a quick check before a judging run:

```powershell
python -m unittest discover -s tests -p test_torture_harness.py -v
python torture.py --iterations 5 --seed 12345 --mode basic
python torture.py --iterations 1 --seed 12345 --mode large-value
python torture.py --iterations 1 --seed 12345 --mode max-batch
```

Illustrative output (timing, ACK counts, and outcomes vary by run):

```text
Aid_Map Crash Torture
Mode: basic
Seed: 12345
Iterations: 5
Writer count: 2
Independent oracle: enabled (fsynced JSONL)
Database/artifact location: C:\Users\Judge\AppData\Local\Temp\aid-map-torture-example
Retention: removed after complete PASS; retained otherwise
ITER 1/5 PASS delay=2ms ACKed/sent=2/3 pending_at_kill=yes verification=recovered state matches an allowed serialized prefix
SUMMARY PASS=5 FAIL=0 INCONCLUSIVE=0 ERROR=0 seed=12345 elapsed=1.42s verdict=PASS
```

`PASS` means the recovered state matches an allowed state derived from the
independently fsynced request/ACK oracle. `FAIL` means the recovered state
conflicts with that history. `INCONCLUSIVE` means the available history cannot
establish a safe verdict; it is not a pass. `ERROR` identifies a harness or
environment problem, such as a missing prerequisite or subprocess failure.
Only a complete PASS exits with code 0. A pending request means the harness
had sent a request but had not observed its response at kill time; it does not
prove the process was inside `os.write()` or `fsync()`.

The default run uses unique temporary database directories and removes them
only after a complete PASS. On failure or inconclusive results it retains the
database, oracle log, and `failure.json`, and prints their location. To retain
artifacts for every outcome, select a parent directory:

```powershell
python torture.py --iterations 5 --seed 12345 --mode basic --workdir .\judge-artifacts
```

Reproduce a finding by rerunning the same mode, seed, and iteration count.
Request generation is seeded, but operating-system scheduling means crash
timing and therefore the exact observed history may differ.

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

The database API accepts `Database(path, max_bytes=N)` (or
`Database.open(path, max_bytes=N)`) to cap the database file size. The limit
accounts for complete encoded records, including record headers, and is checked
under the database's process lock before each write. The CLI `open` command
accepts `python -m src.cli open PATH --max-bytes N` to open with that limit;
the setting applies to that database instance and is not stored in the database
file. Omitting it preserves the existing unlimited behavior.

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
order. `large-value` runs maximum-size PUT, overwrite, and delete crash cases;
`max-batch` sends a mixed, repeated-key batch of exactly 10,000 operations.
Both heavy modes are limited to five iterations per run. A heavy-mode case is
reported as INCONCLUSIVE if its target request had already been ACKed when the
process was killed; server liveness alone is not treated as proof that an
operation was pending. `recovery-crash`, `compaction-crash`, and `disk-full`
modes are currently NOT IMPLEMENTED.
