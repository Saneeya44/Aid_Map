# Aid_Map

Aid_Map is a small Python append-only key-value database project for the
"Kill It and Prove It" hackathon. This README describes the current code; it
does not certify compliance with an official challenge statement that is not
included in this repository. See [REQUIREMENTS_CHECKLIST.md](./REQUIREMENTS_CHECKLIST.md)
for the evidence and blocked requirements.

## Quick Judge Start

### Prerequisites

- Python 3.7 or newer for the CLI and torture harness (standard-library APIs
  used by those programs are available from Python 3.7).
- Python 3.9 or newer to run the complete current test suite; at least one
  test uses `str.removesuffix`, introduced in Python 3.9.
- The repository files, and a writable location for temporary databases.
- No third-party Python packages are required. No build or package-install
  step is needed.

This checkout's earlier recorded environment was Python 3.15.0; the minimum
version statement above is based on the source APIs and syntax, not a separately
executed compatibility matrix.

### Commands

Run from the repository root in Windows PowerShell:

```powershell
python -m src.cli --help
python torture.py --iterations 5 --seed 12345 --mode basic
```

Default judge command:

```powershell
python torture.py --iterations 500 --seed 12345 --mode basic
```

The harness also supports `large-value` and `max-batch` modes (each limited to
five iterations per invocation):

```powershell
python torture.py --iterations 1 --seed 12345 --mode large-value
python torture.py --iterations 1 --seed 12345 --mode max-batch
```

`--workdir PATH` retains run artifacts under a newly created unique
subdirectory of PATH. Without it, a temporary directory is used. A complete
PASS is normally cleaned up; failure/inconclusive evidence is retained.
Request generation is seeded, but operating-system scheduling means crash
timing and exact observed history may vary.

The following output is **illustrative only**; counts and timings vary:

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

- **PASS**: recovered data matches a state allowed by the independent,
  fsynced oracle, including permissible outcomes for a request without an
  observed ACK.
- **FAIL**: the recovered state conflicts with the captured history; the
  harness retains evidence.
- **INCONCLUSIVE**: available history cannot prove a safe verdict. This is
  not a pass.
- **ERROR**: harness/environment problem, such as a missing prerequisite or
  subprocess failure.

Only a full PASS exits with code zero. A pending request means it was sent but
its response had not been observed at kill time; it does not prove a kill
occurred inside a write or fsync call. For reproduction, rerun with the same
mode, seed, and iteration count; scheduling may still alter the observed
crash window.

## Offline setup

There is no compilation or dependency installation step. With Python already
installed, run from the repository root:

```powershell
python -m src.cli --help
```

This prints CLI usage and does not open a database. The commands below that
omit `--db` use a persistent user-data database, so use a disposable explicit
path when experimenting.

## CLI commands

All commands accept `--db PATH`. For compatibility, path-first positional
forms are also accepted, as shown below. If a database path is omitted, the
default is `%LOCALAPPDATA%\Aid_Map\aid_map.kv` on Windows and
`$XDG_DATA_HOME/Aid_Map/aid_map.kv` (or `~/.local/share/Aid_Map/aid_map.kv`)
on other platforms. A path's `.lock` sidecar is used for cross-process locking.

| Command | Recommended syntax | Behavior |
|---|---|---|
| PUT | `python -m src.cli put --db PATH KEY VALUE` | UTF-8 encodes key/value, writes the value, prints `OK` |
| GET | `python -m src.cli get --db PATH KEY` | Prints UTF-8 value or `NOTFOUND` |
| DELETE | `python -m src.cli del --db PATH KEY` | Deletes the key if present and prints `OK` |
| BATCH | `python -m src.cli batch --db PATH BATCH_FILE` | Reads `PUT KEY VALUE` / `DEL KEY` lines and submits one batch |
| OPEN | `python -m src.cli open --db PATH` | Opens/replays the database, prints live-key count and elapsed time |
| DUMP | `python -m src.cli dump --db PATH` | Prints sorted live entries as Base64 |
| CHECK | `python -m src.cli check --db PATH` | Strictly scans for integrity errors without recovering or repairing the data file |
| SERVE | `python -m src.cli serve --db PATH` | Reads JSONL requests until EOF and emits ACK/ERR lines |

Equivalent legacy path-first forms include:

```powershell
python -m src.cli put PATH KEY VALUE
python -m src.cli get PATH KEY
python -m src.cli del PATH KEY
python -m src.cli batch PATH BATCH_FILE
python -m src.cli open PATH
python -m src.cli dump PATH
python -m src.cli check PATH
python -m src.cli serve PATH
```

A batch file contains one operation per nonblank line. PUT values may contain
spaces after the key:

```text
PUT name Ada Lovelace
DEL old-name
```

The engine limits keys to 1-1024 bytes, values to at most 1 MiB, and batches
to at most 10,000 operations. The CLI's text operands are UTF-8; use SERVE for
arbitrary binary keys and values.

## Safe manual demonstration

This PowerShell example uses a fresh temporary directory rather than any
project-root `.kv` file. Leave the directory in place until you have finished
inspecting it; then remove only that displayed temporary directory if desired.

```powershell
$demoDir = Join-Path ([System.IO.Path]::GetTempPath()) ("aid-map-demo-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $demoDir | Out-Null
$db = Join-Path $demoDir "demo.kv"

python -m src.cli put --db $db name Rahul
python -m src.cli get --db $db name
python -m src.cli open --db $db
python -m src.cli check --db $db
python -m src.cli dump --db $db
python -m src.cli del --db $db name
python -m src.cli get --db $db name
```

Expected output includes `OK`, `Rahul`, database statistics, `OK: database
integrity verified`, a Base64 dump line before deletion, then `OK` and
`NOTFOUND`. Do not replace `$db` with an existing project database unless you
intend to change it.

## SERVE protocol

SERVE reads one JSON object per stdin line through EOF. Request `key` and
`value` fields are canonical strict RFC 4648 Base64 strings. Operation IDs
must be nonempty printable ASCII without whitespace; `-` is reserved for an
unusable request ID in an error response.

```json
{"id":"op-1","op":"put","key":"bmFtZQ==","value":"QW5hbnlh"}
{"id":"op-2","op":"delete","key":"bmFtZQ=="}
{"id":"op-3","op":"batch","operations":[{"op":"put","key":"YQ==","value":"MQ=="},{"op":"delete","key":"Yg=="}]}
```

Keys decode to 1-1024 bytes; values decode to 0-1 MiB; a SERVE batch contains
1-10,000 operations. The complete request and complete batch are validated
before execution. Successful calls produce one `ACK <id>` line; invalid
requests and failed calls produce `ERR <id> <single-line reason>` (or
`ERR - <reason>` when no usable ID exists). Responses are flushed immediately;
diagnostics go to stderr. Deleting a missing key is acknowledged because it
returns successfully and leaves the key absent.

The current engine calls `os.fsync()` on the database file after writing each
non-empty record and before the database method returns. SERVE emits ACK after
the method returns. This is the implementation's ordering, not a universal
power-loss guarantee: filesystem/hardware behavior and directory-entry
durability are not fully established.

## On-disk format and recovery

The database is an append-only sequence of records; it has no global file
header or separately persisted index. Each record starts with a 14-byte
big-endian header: four-byte `KVDB` magic, one-byte version, one-byte record
type, four-byte payload length, and four-byte CRC-32. The checksum covers the
version, record type, and payload, but not the magic or payload-length field.
Record types are PUT (1), DELETE (2), and BATCH (3).

PUT payloads contain a two-byte key length, key, then value. DELETE payloads
contain a two-byte key length and key. BATCH payloads start with a four-byte
operation count and contain typed operations with two-byte key lengths; PUT
members also contain a four-byte value length and value. See
[ARCHITECTURE.md](./ARCHITECTURE.md) for field layouts, operation ordering,
and limits.

On open, the engine replays records to rebuild a per-process in-memory
dictionary. An incomplete trailing header or payload is truncated to the
start of that record and fsynced; invalid headers/checksums raise errors.
`check` uses a strict read-only scan and does not repair the file. `dump`
prints each live entry as `key=<Base64> value=<Base64>`.

The process-local `threading.RLock` and cross-process sidecar lock serialize
operations. Windows uses `msvcrt.locking`; POSIX uses `fcntl.flock`. The
implementation currently has no compaction, configured maximum database
size, or million-key recovery benchmark. `os.fsync()` is called for record
writes and tail repair; no directory fsync is performed for newly created
files.

## Torture harness modes

- `basic`: concurrent seeded PUT/DELETE/BATCH requests, force-kill, then
  separate `open`, `check`, and `dump` subprocesses and oracle verification.
- `large-value`: 1 MiB PUT/overwrite/delete cases; maximum five iterations.
- `max-batch`: 10,000-operation mixed batch; maximum five iterations.
- `recovery-crash`, `compaction-crash`, and `disk-full`: **NOT IMPLEMENTED**.

The oracle is maintained separately from the `.kv` database and fsyncs its
events. The verifier allows an unACKed operation to have committed or not and
may report INCONCLUSIVE where the history cannot justify a binary verdict.
Heavy modes do not claim a kill during a write/fsync merely because the server
process remained alive. The harness uses temporary paths by default and does
not use the project-root `.kv` databases.

## Platform notes and limitations

Use PowerShell from the repository root on Windows; the commands above use
PowerShell syntax. `src/locking.py` has Windows and POSIX locking paths, but
the filesystem's semantics, Python distribution, and process-kill behavior
can affect test timing. No cross-platform compatibility matrix or guarantee
for network filesystems is documented. Avoid running broad legacy tests
without confirming their database paths; use temporary paths for manual or
crash testing.

The implementation must not be described as fully crash-safe or as meeting
the judging criteria solely because CLI/harness tests pass. Consult
[REQUIREMENTS_CHECKLIST.md](./REQUIREMENTS_CHECKLIST.md) and obtain the exact
official scenario and performance requirements before making those claims.
