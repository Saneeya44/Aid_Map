# Aid_Map

Aid_Map is a small Python append-only key-value database project for the
"Kill It and Prove It" hackathon. This README describes current code and its
known limitations; it is not a blanket claim that every crash scenario passes.
See [REQUIREMENTS_CHECKLIST.md](./REQUIREMENTS_CHECKLIST.md) for evidence and
remaining gaps.

The CLI and SERVE adapter in `src/cli.py` call the `Database` API in
`src/database.py`. Records are encoded, appended and fsynced by
`src/storage.py`; `src/locking.py` serializes threads and processes. The index
is rebuilt in memory by replaying the authoritative database log. Compaction
rewrites a temporary snapshot and replaces the log. No third-party runtime
packages are required.

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
Set-Location D:\Aidmap_sit
python --version
python -m src.cli --help
python torture.py --iterations 5 --seed 12345 --mode basic
```

Default judge command:

```powershell
python torture.py --iterations 500 --seed 12345 --mode basic
```

The harness also supports `large-value`, `max-batch`, `recovery-crash`,
`compaction-crash`, and `disk-full` modes. Heavy modes are bounded to five
iterations per invocation.

```powershell
python torture.py --iterations 1 --seed 12345 --mode large-value
python torture.py --iterations 1 --seed 12345 --mode max-batch
python torture.py --iterations 5 --seed 12345 --mode compaction-crash
python torture.py --iterations 5 --seed 12345 --mode disk-full
python torture.py --iterations 3 --seed 12345 --mode recovery-crash
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

### Recorded performance results

The following team-recorded benchmark measurements are included as reported;
the benchmark scripts are in the repository, but a raw console transcript is
not currently checked in:

- One million live keys inserted in **54.554 seconds**.
- One-million-key recovery in **0.809168 seconds**; all 1,000,000 keys were
  present and sampled values matched.
- Compaction of that all-live workload changed the file from **31,001,800**
  to **40,000,000 bytes**. This input contained no obsolete records, so this
  is not the churn-shrinkage acceptance test; snapshot PUT records can be
  larger than a workload stored in batches.
- Recovery after that compaction took **1.627460 seconds**, with all million
  keys present.
- Churn workload: **1,000,000 puts + 1,000,000 deletes**, zero live keys,
  **51,003,600 → 0 bytes** after compaction (**100% reclaimed**), in
  **166.26 seconds**; reopening afterward passed.
- Full unit suite: **111 tests, OK**, **8.197 seconds**.
- Basic crash torture: **500 PASS, 0 FAIL, 0 INCONCLUSIVE, 0 ERROR**, seed
  `67890`, **194.24 seconds**.

The fresh full-suite run for this integration pass also passed all 111 tests;
unittest reported **14.590 seconds**. It ran from an isolated working
directory so legacy import-time test scripts could not modify repository
`.kv` files. Recovery-crash passed three iterations with seed `12345` in
**1.64 seconds**, with the replay pause and force-kill confirmed in each.
These are process-kill tests, not power-loss or hardware-cache tests.

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
| COMPACT | `python -m src.cli compact --db PATH` | Atomically replaces the log with a snapshot of live keys |

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
python -m src.cli compact PATH
```

A batch file contains one operation per nonblank line. PUT values may contain
spaces after the key:

```text
PUT name Ada Lovelace
DEL old-name
```

For an instance-specific database size limit, OPEN accepts
`python -m src.cli open PATH --max-bytes N` (or
`python -m src.cli open --db PATH --max-bytes N`). The API also accepts
`Database(path, max_bytes=N)` or `Database.open(path, max_bytes=N)`. The limit
counts complete encoded records including their headers, is checked while
holding the database process lock, and is not persisted in the database file.
If a new record would exceed the limit, that append is rejected before writing;
if an OS write fails after a partial append, the implementation attempts to
truncate back to the original length and fsync. Omitting the option preserves
unlimited size.

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
@'
PUT city Wellington
DEL name
'@ | Set-Content -Encoding ascii (Join-Path $demoDir "changes.txt")
python -m src.cli batch --db $db (Join-Path $demoDir "changes.txt")
python -m src.cli dump --db $db
python -m src.cli compact --db $db
python -m src.cli check --db $db
```

Expected output includes `OK`, `Rahul`, database statistics,
`OK: database integrity verified`, and Base64 dump lines; exact counts/bytes are
**illustrative**, not fixed. Do not replace `$db` with an existing project
database unless you intend to change it. After compaction, `$db` contains the
current live state only.

The size cap is a per-`Database` instance option; the CLI exposes it on `open`
as `python -m src.cli open --db $db --max-bytes 1048576`. That command reports
stats and closes the instance; subsequent CLI write commands do not accept a
size-cap option. To perform capped writes, use the API:

```powershell
$env:AIDMAP_DEMO_DB = $db
@'
import os
from src.database import Database

with Database.open(os.environ["AIDMAP_DEMO_DB"], max_bytes=1048576) as db:
    db.put(b"capped-key", b"capped-value")
    print("live keys:", db.key_count())
'@ | python -
Remove-Item Env:AIDMAP_DEMO_DB
```

Batch operations are applied in their supplied order inside one BATCH record.
For repeated operations on one key, the last operation determines its final
state (a final DELETE leaves it absent).

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

### Compaction

Run `python -m src.cli compact PATH` (or `python -m src.cli compact --db PATH`)
to reclaim space used by obsolete records and deleted-key tombstones.
Compaction takes the process lock, recovers the current live index, writes a
snapshot of live PUT records to a temporary file in the database directory,
flushes and fsyncs that file, then atomically replaces the database with
`os.replace`. On non-Windows systems it also fsyncs the containing directory
after replacement. A kill before replacement leaves the original database
authoritative; after replacement the compacted log is authoritative. A
temporary file left by a force-kill is not referenced during recovery and is
ignored (automatic cleanup of every orphan is not guaranteed).

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
operations. Windows uses `msvcrt.locking`; POSIX uses `fcntl.flock`.
`--max-bytes` applies to the OPEN-created Database instance; it is not stored
in the file and therefore must be supplied again when reopening with the same
limit. The API accepts it for write instances; the CLI exposes the option on
`open` only. Million-key benchmark results are recorded above.
`os.fsync()` is called for record writes and tail repair; database-file
creation is not followed by directory fsync. Compaction does fsync the
containing directory after replacement on non-Windows systems; Windows uses
`os.replace` but has no corresponding directory fsync call.

## Torture harness

- `basic`: concurrent seeded PUT/DELETE/BATCH requests, force-kill, then
  separate `open`, `check`, and `dump` subprocesses and oracle verification.
- `large-value`: 1 MiB PUT/overwrite/delete cases; maximum five iterations.
- `max-batch`: 10,000-operation mixed batch; maximum five iterations.
- `compaction-crash`: kills a subprocess during snapshot-record generation,
  before replacement, or after replacement, then checks recovered live data;
  after replacement it also checks that the file shrank.
- `disk-full`: fault-injects ENOSPC after a partial record write and checks
  for SERVE `ERR` (not ACK) and preservation of prior committed state.
- `recovery-crash`: seeds acknowledged PUTs and an atomic batch, appends an
  incomplete tail, then launches recovery in a child process. The child is
  force-killed only after it reports pausing inside record replay; a fresh
  `open`, `check`, and `dump` is then compared with the fsynced ACK oracle.
  The mode reports INCONCLUSIVE (never PASS) if the replay pause is not
  observed. It is limited to five iterations.

The oracle is maintained separately from the `.kv` database and fsyncs its
events. The verifier allows an unACKed operation to have committed or not and
may report INCONCLUSIVE where the history cannot justify a binary verdict.
Heavy modes do not claim a kill during a write/fsync merely because the server
process remained alive. The harness uses temporary paths by default and does
not use the project-root `.kv` databases.

Example targeted commands:

```powershell
python torture.py --mode compaction-crash --iterations 5 --seed 12345
python torture.py --mode disk-full --iterations 5 --seed 12345
python torture.py --mode recovery-crash --iterations 1 --seed 12345
```

The disk-full mode simulates a partial-write ENOSPC error; it does not exhaust
a real device or test every filesystem/device failure. Separate
`tests/test_max_bytes.py` cases exercise configured limits and partial-write
rollback.

## Offline judge demonstration

Use the temporary-directory procedure in [JUDGE_DEMO.md](./JUDGE_DEMO.md).
It exercises only disposable files and standard-library commands. Do not run
the million-key benchmarks during the live presentation; use the recorded
measurements above.

## Platform notes and limitations

Use PowerShell from the repository root on Windows; the commands above use
PowerShell syntax. `src/locking.py` has Windows and POSIX locking paths, but
the filesystem's semantics, Python distribution, and process-kill behavior
can affect test timing. No cross-platform compatibility matrix or guarantee
for network filesystems is documented. Avoid running broad legacy tests
without confirming their database paths; use temporary paths for manual or
crash testing.

The verifier requires ACKed writes and allows an eligible unACKed request to
have committed or not; it reports INCONCLUSIVE rather than guessing when
history cannot establish a safe serialized result. Neither CLI tests nor
torture-mode success alone proves all crash-safety requirements. See
[REQUIREMENTS_CHECKLIST.md](./REQUIREMENTS_CHECKLIST.md) for known gaps.
