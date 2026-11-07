# Aid_Map Requirements Checklist

## Evidence boundary

The official hackathon problem statement and the text of scenarios A1-A7,
B1-B6, C1-C6, D1-D4, E1-E5, and F1-F4 were not found in the repository at
review time. The 32 scenario identifiers are known, but their required
preconditions, crash points, and pass criteria are not. Consequently this
checklist is a source-backed implementation inventory, **not a certification
against the complete official statement**. Every official requirement and
scenario mapping remains blocked until the team supplies the exact source
document.

Statuses:

- **VERIFIED**: directly exercised in a test or command whose result is
  recorded below.
- **IMPLEMENTED BUT UNVERIFIED**: current source contains the behavior; no
  complete validation was run for this checklist.
- **MISSING**: the current implementation does not provide the feature.
- **BLOCKED**: the requirement or acceptance criteria cannot be established
  from available material.

No product or durability capability is marked VERIFIED by presence in source
alone. This documentation pass ran only command-help checks, not the unit or
crash suites.

## Engine

| Requirement / capability visible in the repository | Status | Evidence |
|---|---|---|
| Append-only records with PUT, DELETE, and BATCH record types | IMPLEMENTED BUT UNVERIFIED | `src/storage.py:encode_record`, constants and payload handling in `src/database.py`; related tests in `tests/test_storage.py`, `tests/test_database.py` |
| Validate key length (1-1024 bytes), value maximum (1 MiB), and batch maximum (10,000) | IMPLEMENTED BUT UNVERIFIED | `src/database.py:_encode_put`, `_encode_delete`, `_encode_batch`; `tests/test_torture_scenarios.py` |
| Recover the in-memory index by replaying records | IMPLEMENTED BUT UNVERIFIED | `src/database.py:Database._recover`; `tests/test_recovery.py`, `tests/test_database_api.py` |
| Repair only an incomplete trailing record by truncating at its starting offset and fsyncing | IMPLEMENTED BUT UNVERIFIED | `src/storage.py:_read_records(repair_incomplete=True)`; `tests/test_check_readonly.py:test_normal_recovery_still_repairs_incomplete_tail` |
| Detect invalid header/checksum without recovery repair in strict CHECK | IMPLEMENTED BUT UNVERIFIED | `src/storage.py:verify_records`; `src/database.py:Database.check_file`; `tests/test_check_readonly.py` |
| Serialize process/thread access using an RLock plus a sidecar process lock | IMPLEMENTED BUT UNVERIFIED | `src/database.py` operations; `src/locking.py:FileLock`; `tests/test_locking.py`, `tests/test_thread_safety.py`, `tests/test_multiprocessing.py` |
| Configurable database size cap / `--max-bytes` | MISSING | No configuration or limit found in `src/`; test fault injection is not a size-cap feature |
| Compaction or log rewrite | MISSING | No compaction implementation, rename/replace flow, or compaction tests found |
| Performance target for recovery at one million keys | BLOCKED | No official threshold or benchmark evidence supplied; source replay is linear in records and no million-key performance result is documented |
| Complete official engine requirements | BLOCKED | Official problem statement was not found; supply exact text and acceptance criteria |

## CLI

| Requirement / capability visible in the repository | Status | Evidence |
|---|---|---|
| Eight commands: `put`, `get`, `del`, `batch`, `open`, `dump`, `check`, `serve` | IMPLEMENTED BUT UNVERIFIED | `src/cli.py:main`; `tests/test_cli.py`, `tests/test_serve.py` |
| Text CLI key/value arguments are UTF-8 encoded | IMPLEMENTED BUT UNVERIFIED | `src/cli.py:main`; `tests/test_cli.py` |
| Batch file parsed and fully collected before one engine batch call | IMPLEMENTED BUT UNVERIFIED | `src/cli.py:read_batch_operations`, `main`; `tests/test_cli.py` |
| SERVE accepts JSONL, strict Base64, validates requests, and responds with ACK/ERR | IMPLEMENTED BUT UNVERIFIED | `src/cli.py:_parse_serve_request`, `serve`; `tests/test_serve.py` |
| DUMP uses Base64 key/value lines | IMPLEMENTED BUT UNVERIFIED | `src/cli.py:main` dump branch; output format `key=<Base64> value=<Base64>` |
| CHECK scans through `Database.check_file()` without constructing a recovering Database | IMPLEMENTED BUT UNVERIFIED | `src/cli.py:main` check branch; `src/database.py:Database.check_file`; `tests/test_check_readonly.py` |
| Exact compatibility with the judging CLI output contract | BLOCKED | No official statement/output oracle supplied; README describes the current implementation, not official compliance |

## Torture harness

| Requirement / capability visible in the repository | Status | Evidence |
|---|---|---|
| Seeded basic, maximum-value, and maximum-batch modes | IMPLEMENTED BUT UNVERIFIED | `torture.py:run_basic`, `run_heavy_mode`; `tests/test_torture_harness.py` |
| Independent JSONL oracle flushes and fsyncs each event | IMPLEMENTED BUT UNVERIFIED | `torture.py:OracleLog.record`; test `test_verifier_uses_fsynced_ack_events_from_oracle_log` |
| Oracle validates allowed serialized state and atomic batch application | IMPLEMENTED BUT UNVERIFIED | `torture.py:apply_request`, `verify_oracle`, `verify_oracle_events`; negative/positive tests in `tests/test_torture_harness.py` |
| Bounded subprocesses and force-kill behavior | IMPLEMENTED BUT UNVERIFIED | `torture.py:run_cli`, `IterationRunner`; `tests/test_torture_scenarios.py` |
| `recovery-crash`, `compaction-crash`, and `disk-full` harness modes | MISSING | `torture.py:SUPPORTED_MODES` excludes them; CLI reports unsupported modes |
| Full judging-run success at the official iteration count and environment | BLOCKED | No official environment criteria or full 500-iteration result recorded in this documentation pass |

## Documentation and submission

| Requirement | Status | Evidence |
|---|---|---|
| Accurate source-derived architecture, file format, write ordering, and limitations | IMPLEMENTED BUT UNVERIFIED | `ARCHITECTURE.md`; team must compare with the official requirements |
| Exact 32 official scenario descriptions and mapping | BLOCKED | Only scenario IDs were available; full scenario text is required |
| Prompt provenance / complete teammate prompt history | BLOCKED | No `PROMPTS.md` or original prompt archive existed; the new file is a collection template only |
| Official submission packaging and judging criteria | BLOCKED | No official submission checklist or challenge document in the workspace |

## Performance

| Requirement | Status | Evidence |
|---|---|---|
| One-million-key recovery within the official time/memory limit | BLOCKED | Official threshold and benchmark run absent |
| Maximum batch behavior | IMPLEMENTED BUT UNVERIFIED | Limits in `src/cli.py` and `src/database.py`; `tests/test_torture_scenarios.py:test_engine_accepts_10000_batch_operations_and_rejects_10001` |
| Bounded heavy-mode iterations | IMPLEMENTED BUT UNVERIFIED | `torture.py:MAX_HEAVY_ITERATIONS`; maximum-value cases append several 1 MiB values |

## Demo

| Requirement / capability | Status | Evidence |
|---|---|---|
| Disposable-path CLI and SERVE examples | IMPLEMENTED BUT UNVERIFIED | `README.md`; no demo was run during this documentation pass |
| Demonstration does not use or overwrite repository-root `.kv` files | IMPLEMENTED BUT UNVERIFIED | README demo uses a unique temporary directory; code defaults remain persistent if the user omits a path |
| Demo covers every official judging requirement | BLOCKED | Official demo rubric and scenario descriptions are unavailable |

## Validation performed for this document update

- Read-only inspection of current source, tests, README, repository status, and
  recent commit subjects.
- `python -m src.cli --help` and `python torture.py --help` are the permitted
  lightweight validation commands to run after editing.
- No engine, CLI, recovery, storage, lock, or torture correctness suite is
  represented as run by this documentation update.

## Inputs required to finalize

1. The complete official problem statement, including mandatory performance,
   platform, and submission requirements.
2. The exact text and expected disk state/pass criteria for all 32 scenarios.
3. The judging environment and required Python version/platform matrix.
4. The official recovery performance threshold and a representative dataset.
5. Any teammate-owned demonstration or submission checklist.
