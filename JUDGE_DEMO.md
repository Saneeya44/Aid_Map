# Aid_Map: 15-Minute Offline Judge Demo

Run these commands from the repository root in Windows PowerShell. No network
access or third-party Python packages are needed. Do not run this demo against
an existing project `.kv` file.

## 1. Prepare a disposable database

```powershell
Set-Location D:\Aidmap_sit
python --version
python -m src.cli --help
$demoDir = Join-Path ([System.IO.Path]::GetTempPath()) ("aid-map-demo-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $demoDir | Out-Null
$db = Join-Path $demoDir "demo.kv"
```

The commands below are real CLI forms. Output blocks are **illustrative**;
open times, file sizes, and dump contents depend on the run.

## 2. Show the CLI and persistent state

```powershell
python -m src.cli put --db $db name Rahul
python -m src.cli get --db $db name
python -m src.cli open --db $db
python -m src.cli check --db $db
python -m src.cli dump --db $db
python -m src.cli del --db $db name
python -m src.cli get --db $db name
```

Illustrative output:

```text
OK
Rahul
Live keys: 1
Open/recovery time (includes initialization): <measured milliseconds> ms
OK: database integrity verified
key=bmFtZQ== value=UmFodWw=
OK
NOTFOUND
```

Batch file input is one operation per line:

```powershell
@"
PUT city Wellington
PUT greeting Hello from Aid_Map
DEL greeting
"@ | Set-Content -Encoding ascii (Join-Path $demoDir "changes.txt")
python -m src.cli batch --db $db (Join-Path $demoDir "changes.txt")
python -m src.cli dump --db $db
python -m src.cli compact --db $db
python -m src.cli check --db $db
```

Batch members run in input order within one record; the last operation on a
repeated key determines its final state. Compaction writes a snapshot of live
keys and replaces the database log. The temporary directory and database are
disposable; do not remove them until the demonstration is finished.

## 3. Demonstrate SERVE, bounded writes, and torture

SERVE accepts one JSON object per line. Key/value bytes are strict Base64;
each request gets one `ACK <id>` or `ERR <id> <reason>` line:

```powershell
@'
{"id":"demo-1","op":"put","key":"Y291bnRlcg==","value":"MQ=="}
{"id":"demo-2","op":"batch","operations":[{"op":"put","key":"Y291bnRlcg==","value":"Mg=="},{"op":"delete","key":"Y2l0eQ=="}]}
'@ | python -m src.cli serve --db $db
```

Illustrative response:

```text
ACK demo-1
ACK demo-2
```

The `--max-bytes` option exists on the CLI `open` command for that opened
instance, but CLI PUT/DELETE/BATCH subcommands do not expose a cap. To show a
capped write, use the actual API:

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

For short, deterministic test demonstrations, use disposable temporary paths
created internally by the harness:

```powershell
python torture.py --iterations 5 --seed 12345 --mode basic
python torture.py --iterations 3 --seed 12345 --mode recovery-crash
python torture.py --iterations 1 --seed 12345 --mode compaction-crash
python torture.py --iterations 1 --seed 12345 --mode disk-full
```

Other supported bounded modes are `large-value` and `max-batch` (maximum five
iterations per invocation). The same seed reproduces generated requests and
target selection; process scheduling can still change timing. The independent
oracle is stored separately from the database, and each event is flushed and
fsynced. The verifier requires ACKed operations and allows an eligible
unACKed request either to have committed or not. A pending request does not
prove the kill landed inside `os.write` or `fsync`.

## 4. Explain the guarantees and limitations

- **fsync / ACK:** a successful non-empty append calls `os.fsync` before the
  database method returns; SERVE sends ACK only after return. That is the
  code-path ordering tested here, not a universal hardware or power-loss
  guarantee.
- **Atomic batch:** one batch is encoded as one BATCH record. A short trailing
  record is truncated as a whole; replay applies a complete valid record in
  order. The format has no separate commit marker.
- **Recovery:** the live index is rebuilt by scanning the log. Recovery
  repairs only an incomplete final record; invalid headers/checksums are
  errors. CHECK is a strict, read-only scan.
- **Compaction:** writes and fsyncs a same-directory temporary snapshot,
  `os.replace`s the database, and fsyncs the parent directory on non-Windows.
  Windows has no directory-fsync step in this implementation.
- **Disk full:** append checks `max_bytes` before writing. A partial OS write
  error triggers truncate-to-old-size plus fsync; rollback failure is
  reported. The harness fault-injects ENOSPC; it does not fill a physical disk.
- **Concurrency:** an instance `threading.RLock` plus a sidecar process lock
  serializes writers and readers using these APIs.
- **Independent ACK oracle:** the harness records request, send, and response
  events in an fsynced JSONL file separate from the `.kv` database.
- **TTL:** no TTL feature exists. Proposed design: versioned PUT records store
  an absolute UTC deadline, reads treat `now >= deadline` as expired, and
  compaction omits expired values. State explicitly that clock rollback can
  delay expiry; format migration and deterministic clock tests are required.

Three “crash here” explanations:

1. **Mid-record append:** a force-kill can leave a torn tail; recovery truncates
   the whole final record. An append exception attempts immediate rollback.
2. **After file fsync, before ACK:** the data record may survive although a
   client never observed its response. The oracle treats this as an
   unacknowledged, potentially committed request.
3. **Compaction replacement:** before `os.replace`, the old file is
   authoritative; after it, the snapshot is authoritative. The harness
   exercises pauses around that transition, but not a power cut inside the OS
   replacement operation.

## 5. Verified results (do not rerun these during the presentation)

The results below are the supplied measured results. Million-key benchmark
scripts are present, but raw console transcripts are not checked into the
repository.

- Full unit suite: **111 tests, OK**, **8.197 seconds**.
- Fresh isolated full-suite run during this integration pass: **111 tests,
  OK**, unittest-reported **14.590 seconds**. The different timing is from a
  separate run; both suites passed.
- Basic crash torture: **500 PASS, 0 FAIL, 0 INCONCLUSIVE, 0 ERROR**, seed
  `67890`, **194.24 seconds**.
- Recovery-crash mode: three seeded smoke iterations passed in the
  current documentation validation with seed `12345` in 1.64 seconds. Each
  iteration confirmed a pause inside replay, killed the child, then passed
  open/CHECK/DUMP/oracle verification. No standalone log file is checked in.
- One million live keys inserted in **54.554 seconds**; one-million-key
  recovery **0.809168 seconds**, with 1,000,000 live keys and sampled values
  correct.
- All-live compaction: **31,001,800 → 40,000,000 bytes**; it grew because the
  input had no obsolete records and was batch-encoded. This is not the churn
  shrinkage acceptance test. Recovery after compaction: **1.627460 seconds**,
  all million keys present.
- Churn: **1,000,000 puts + 1,000,000 deletes**, zero live keys,
  **51,003,600 → 0 bytes**, **100% reclaimed**, **166.26 seconds**; reopen
  passed.

## Suggested 15-minute flow and speaking roles

| Time | Demo | Speaker cue |
|---|---|---|
| 0:00-1:00 | State the problem and show `--help`. | Presenter: durable embedded KV, small Python implementation, no external packages. |
| 1:00-4:00 | PUT/GET/DELETE, batch file, DUMP and CHECK. | CLI owner: UTF-8 CLI, Base64 dump, disposable data path. |
| 4:00-6:00 | SERVE JSONL and ACK/ERR. | Protocol owner: strict Base64 input, one response per ID, ACK follows method return. |
| 6:00-9:00 | Explain record header, fsync, BATCH, locks and recovery. | Engine owner: 14-byte header, record replay, whole-record tail repair; be explicit that there is no commit marker. |
| 9:00-11:00 | Run basic and recovery-crash smoke. | Test owner: independent fsynced oracle and confirmed replay-pause handshake; results do not prove power-loss safety. |
| 11:00-13:00 | Explain compaction, max-bytes and injected ENOSPC. | Engine owner: temp snapshot/fsync/replace and rollback; note Windows directory-fsync limitation. |
| 13:00-14:00 | Present measured million-key and churn numbers. | Benchmark owner: distinguish all-live compaction growth from 100% churn reclamation; do not rerun expensive work. |
| 14:00-15:00 | State gaps and take questions. | Team: B2 marker mismatch, F4 tail-truncation risk, platform and prompt-history gaps. |

Keep the demo directory path in `$demoDir` until the judges have finished
inspecting the disposable database. Do not stage benchmark databases or
temporary torture artifacts.
