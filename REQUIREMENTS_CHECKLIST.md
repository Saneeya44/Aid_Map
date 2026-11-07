# Aid_Map Official Requirements Checklist

## Evidence and status rules

The official requirements below are transcribed from the user-provided
“Official Kill It and Prove It — Scenario Reference” (Problem Statement,
Section 5, pages 5–8, plus Additional official requirements). The requested
`OFFICIAL_SCENARIOS.md` file was not found in the workspace; the scenario
reference provided in the conversation was used. Scenario details and current
implementation mapping are in the 32-row matrix in
[ARCHITECTURE.md](./ARCHITECTURE.md).

**Status meanings**

- **VERIFIED** — matching behavior was exercised and the test result is
  recorded.
- **NOT VERIFIED** — source or related tests exist, but this exact official
  criterion has not been demonstrated by evidence reviewed here.
- **NOT IMPLEMENTED** — the required mechanism is absent or current behavior
  conflicts with the official criterion.
- **BLOCKED** — the official acceptance details needed to judge compliance
  have not been supplied.

No correctness tests were run for this documentation update. Test references
below identify code evidence only, not a new test result. Tests in legacy
scripts that mutate repository-root `.kv` files are not treated as safe
automated verification.

## Engine

| Official requirement | Status | Current evidence and gap |
|---|---|---|
| Keys: 1–1,024 bytes | NOT VERIFIED | `src/database.py:_encode_put`, `_encode_delete`; SERVE checks in `src/cli.py:_parse_serve_request`; limit coverage in `tests/test_serve.py::test_key_value_and_batch_limits_are_validated`. |
| Values: 0–1 MiB | NOT VERIFIED | `src/database.py:_encode_put`; `tests/test_torture_scenarios.py::test_maximum_one_mib_value_survives_restart`; this is not crash-during-write verification. |
| At least 1,000,000 keys | NOT VERIFIED | No explicit engine key-count cap found; no 1,000,000-key capacity or memory test exists. |
| Atomic batches up to 10,000 operations | NOT VERIFIED | Single BATCH record in `src/database.py:Database.batch` and `src/storage.py:append_record`; `tests/test_torture_scenarios.py::test_engine_accepts_10000_batch_operations_and_rejects_10001` covers the boundary, and `test_force_kill_during_partial_batch_keeps_entire_old_state` covers a partial 2-op record. No 10,000-op mid-write crash test. |
| At least two concurrent writers | NOT VERIFIED | `src/locking.py:FileLock` and `src/database.py` operations use a shared sidecar process lock; `tests/test_cross_process_consistency.py` has two-process sequential and barrier-synchronized writes. These named tests were not run in this update. |
| One authoritative database file | NOT VERIFIED | Log in the requested `.kv` path is the persisted data source; index is in memory. `.lock` sidecar is for locking, not data. No snapshot/compaction secondary data file exists. |
| Crash-safe compaction | NOT IMPLEMENTED | No compaction, replacement, rename/swap, or temporary compaction file implementation in `src/`. |
| Configurable maximum file size | NOT IMPLEMENTED | No `--max-bytes`, file cap, or equivalent engine configuration found. ENOSPC fault injection is not a configured size limit. |
| Recovery under 2 seconds with 1,000,000 keys | NOT VERIFIED | `src/database.py:Database._recover` linearly replays records into a Python dictionary; no million-key benchmark or timed recovery test found. |
| BATCH commit-marker ordering required by B2/B3 | NOT IMPLEMENTED | Current format uses one complete CRC-checked BATCH record; it has no separate commit marker. B2’s “fully written but before commit marker means none visible” cannot be implemented by the current record protocol. |
| B5 ordering is defined in README and honored | NOT IMPLEMENTED | `src/database.py:_apply_operations` applies operations in supplied order, but README does not define the result for PUT/DELETE operations on the same key; no focused test for this conflict case was found. |
| F4 one-byte manual truncation loses at most the last unACKed record | NOT IMPLEMENTED | `src/storage.py:_read_records` truncates an incomplete tail to the beginning of that entire record, which may be an ACKed last record. Existing tail-repair test appends `b"KV"` after a valid record, not truncation of an ACKed final record. |

## CLI

| Official requirement | Status | Current evidence and gap |
|---|---|---|
| All eight CLI commands | NOT VERIFIED | `src/cli.py:main` defines `put`, `get`, `del`, `batch`, `open`, `dump`, `check`, and `serve`; fake-database and protocol coverage is in `tests/test_cli.py` and `tests/test_serve.py`. |
| PUT/GET/DELETE/BATCH/OPEN/DUMP/CHECK/SERVE behavior matches official interface | BLOCKED | The supplied official requirements specify the command count but not per-command syntax/output acceptance; full Section 4 details are needed to judge exact compatibility. |

## Torture harness

| Official requirement | Status | Current evidence and gap |
|---|---|---|
| Default: 500 iterations | NOT VERIFIED | `torture.py` argparse default is 500; no 500-iteration result is recorded in this update. |
| Seed is mandatory | NOT VERIFIED | `torture.py` declares `--seed` required; CLI execution not repeated in this update. |
| Independent ACK oracle | NOT VERIFIED | `torture.py:OracleLog.record`, `verify_oracle_log`; `tests/test_torture_harness.py::test_verifier_uses_fsynced_ack_events_from_oracle_log`. Oracle ACK events are flushed/fsynced by source; no new run performed here. |
| Targeted torture modes: large values and large batches | NOT VERIFIED | `SUPPORTED_MODES` contains `large-value` and `max-batch`; heavy modes capped at five iterations. |
| Targeted recovery-crash mode | NOT IMPLEMENTED | `recovery-crash` is listed as known but is not in `SUPPORTED_MODES`; CLI reports it unsupported. |
| Targeted compaction mode | NOT IMPLEMENTED | `compaction-crash` is listed as known but is not in `SUPPORTED_MODES`; engine compaction is also absent. |
| Targeted disk-full mode | NOT IMPLEMENTED | `disk-full` is listed as known but is not in `SUPPORTED_MODES`. A single safe fault-injection test exists for a PUT append error, not a harness mode or configured cap. |
| F1: 500 random kills/restarts maintain invariant | NOT VERIFIED | Basic harness loops isolated runs and checks recovered state against oracle; no 500-iteration result is available as evidence here. |

## Official A1–F4 scenario checklist

`NOT VERIFIED` does not mean the exact scenario passed. See the linked
architecture matrix for official expected outcomes, actual disk/recovery
analysis, source locations, and test names.

| Scenario | Official acceptance criterion (concise transcription) | Status |
|---|---|---|
| A1 | Before any PUT bytes: key absent or previous value | NOT VERIFIED |
| A2 | Partial PUT record: torn bytes ignored; absent or previous value | NOT VERIFIED |
| A3 | Full record before fsync: old or new, no corruption | NOT VERIFIED |
| A4 | After fsync before PUT success return: new value present | NOT VERIFIED |
| A5 | Immediately after PUT success return: new value present | NOT VERIFIED |
| A6 | Kill during DELETE: old value or absent, never corrupted | NOT VERIFIED |
| A7 | Kill during 1 MB PUT: A1–A5 guarantees | NOT VERIFIED |
| B1 | After 3/10 batch operations written: none visible | NOT VERIFIED |
| B2 | After all 10 written, before commit marker: none visible | NOT IMPLEMENTED |
| B3 | After commit marker before fsync: all or none | NOT VERIFIED |
| B4 | After batch success return: all visible | NOT VERIFIED |
| B5 | Same-key PUT/DELETE batch ordering documented and honored | NOT VERIFIED |
| B6 | Halfway through 10,000-op batch: none visible; recovery <2 seconds | NOT VERIFIED |
| C1 | Kill during replay in open: next open correct | NOT VERIFIED |
| C2 | Kill during torn-tail repair: next open succeeds | NOT VERIFIED |
| C3 | Kill during pre-replacement compaction: old file authoritative | NOT IMPLEMENTED |
| C4 | Kill during compaction swap: exactly one complete state authoritative | NOT IMPLEMENTED |
| C5 | Kill after swap before cleanup: intact data; temp files cleaned/ignored | NOT IMPLEMENTED |
| C6 | Kill during close: same guarantees as other kills | NOT VERIFIED |
| D1 | Two writers, different keys: all ACKed PUTs survive kill | NOT VERIFIED |
| D2 | Two writers, same key: one complete value actually written | NOT VERIFIED |
| D3 | Long batch with individual PUTs: atomic batch and ACKed PUTs survive | NOT VERIFIED |
| D4 | GET during batch sees before or after, never partial | NOT VERIFIED |
| E1 | File cap during PUT: clean error; old data readable | NOT IMPLEMENTED |
| E2 | File cap mid-batch: error; no batch operations visible | NOT IMPLEMENTED |
| E3 | Kill after file cap: previously ACKed data survives | NOT IMPLEMENTED |
| E4 | File cap during compaction: no data loss; database opens | NOT IMPLEMENTED |
| E5 | Deletes plus compaction free space for later writes | NOT IMPLEMENTED |
| F1 | 500 random kills/restarts; invariant every iteration | NOT VERIFIED |
| F2 | Repeated kills during recovery; recovery remains idempotent | NOT VERIFIED |
| F3 | Kill/reopen with 1,000,000 keys under 2 seconds; ACKed keys present | NOT VERIFIED |
| F4 | One-byte manual truncation; open succeeds, at most last unACKed record lost | NOT IMPLEMENTED |

## Documentation and submission

| Official requirement | Status | Current evidence and gap |
|---|---|---|
| ARCHITECTURE.md includes an architecture diagram | NOT VERIFIED | Mermaid diagram present in `ARCHITECTURE.md`; no rendering validation in this update. |
| ARCHITECTURE.md includes a 32-row proof matrix | NOT VERIFIED | A1–F4 are listed with official expected state, current disk/recovery mechanism, code locations, and evidence/gaps. |
| ARCHITECTURE.md includes three guarantee arguments | NOT VERIFIED | Durability, batch atomicity, and recoverability sections present; claims remain limited to inspected source. |
| BREAKING.md is at most two printed pages | NOT VERIFIED | `BREAKING.md` exists and is concise in source, but printed page count depends on printer/font/format and was not checked here. |
| BREAKING.md has five required sections | BLOCKED | The names/content rules for those five sections were not included in the supplied official-requirements text; compare with the PDF before claiming compliance. |
| PROMPTS.md contains chronological, original, unedited prompts | NOT VERIFIED | Current `PROMPTS.md` is explicitly a blank collection template; original chronological teammate prompts have not been supplied. |

## Performance and verification gaps

- No million-key dataset or recovery benchmark verifies the 2-second target.
- No kill point is tested at every single-write boundary (before first byte,
  complete write/pre-fsync, post-fsync/pre-return, or during DELETE).
- The batch implementation has no separate commit marker, so B2 is a protocol
  mismatch; 10,000-operation crash timing and the 2-second recovery target are
  unverified.
- Recovery interruption has one test after record scanning, but not repeated
  kills during tail truncation/fsync.
- There is no compaction, configured file-size cap, or the corresponding
  crash/disk-full harness mode.
- Tests named in this document were inspected but not executed during this
  documentation update. In particular, do not run legacy scripts that mutate
  project-root `.kv` files as part of this verification.
