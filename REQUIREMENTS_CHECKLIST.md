# Aid_Map Submission Requirements Checklist

## Status key

- **VERIFIED** — matching source behavior and a test/run or explicit reported
  measurement supports the claim.
- **IMPLEMENTED BUT UNVERIFIED** — source support exists, but required
  scenario-level or platform evidence is incomplete.
- **MISSING** — required implementation or submission material is absent.
- **BLOCKED** — evidence or decision from the team/judges is needed.

The official A1-F4 descriptions and current scenario-by-scenario analysis are
in [ARCHITECTURE.md](./ARCHITECTURE.md). Benchmark figures below were supplied
as recorded team results; their raw console transcripts are not checked into
the current worktree.

## Engine and storage

| Requirement | Status | Evidence / remaining limitation |
|---|---|---|
| Keys are 1-1,024 bytes | VERIFIED | Validation in `src/database.py` and SERVE request parser; limit cases in `tests/test_serve.py`. |
| Values are 0-1 MiB | VERIFIED | Validation in `Database._encode_put` / SERVE; `test_maximum_one_mib_value_survives_restart` covers max-sized value/reopen. Crash-at-each-write-boundary remains unverified (A7). |
| At least 1,000,000 live keys | VERIFIED | Supplied benchmark: one million inserted in 54.554 s; 1,000,000 keys and sampled values correct after recovery. |
| Atomic batches up to 10,000 operations; reject 10,001 | VERIFIED | One BATCH record; `test_engine_accepts_10000_batch_operations_and_rejects_10001`, `test_batch_exceeding_limit_is_rejected_atomically`, and torture verifier/mode tests. Exact 10,000-op mid-write crash and <2 s recovery remain unverified. |
| At least two concurrent writers | VERIFIED | Sidecar process lock; cross-process tests include forced kill and same-key barrier writes. Does not verify every concurrency interleaving. |
| One authoritative database file | VERIFIED | Record log is authoritative; index is memory-only. `.lock` is metadata and compaction temp is transient; replacement changes the authoritative path. |
| Crash-safe compaction | IMPLEMENTED BUT UNVERIFIED | `Database.compact` and atomic same-directory temp + fsync + `os.replace`; compaction kill and failure tests cover before/after replacement. Windows directory fsync is absent; no power-cut test inside OS replacement. |
| Configurable maximum database size | VERIFIED | `Database(..., max_bytes=N)` prechecks complete record size; `tests/test_max_bytes.py` covers cap, batch, concurrent cap and rollback. Option is instance-only, not persisted; CLI exposes it on `open`, not CLI write subcommands. Compaction is not capped. |
| Partial ENOSPC append rollback and no false ACK | VERIFIED | `append_record` truncates to prior size and fsyncs after partial failure; max-bytes tests cover partial PUT/BATCH rollback and SERVE ERR/no ACK. Rollback itself can fail and is explicitly reported. |
| Recovery under 2 seconds with 1,000,000 keys | VERIFIED | Supplied measurement: 0.809168 s, 1,000,000 live keys and sampled values correct. Recovery after all-live compaction: 1.627460 s. |
| B2 separate commit-marker boundary | MISSING | The current format has one checksummed BATCH record and no separate commit marker. A full valid batch can be replayed. |
| Same-key batch ordering defined and honored | IMPLEMENTED BUT UNVERIFIED | Source applies supplied order and README states last operation wins; no focused same-key ordering test found. |
| F4 one-byte truncation loses at most last unACKed record | MISSING | Tail repair discards the whole incomplete final record, potentially including an ACKed record. Exact official condition is not established and may conflict with current design. |

## CLI and interface

| Requirement | Status | Evidence / remaining limitation |
|---|---|---|
| Required eight CLI commands | VERIFIED | PUT, GET, DEL, BATCH, OPEN, DUMP, CHECK, SERVE are implemented and covered by CLI/protocol tests. `compact` is an additional ninth command. |
| Documented syntax matches implementation | VERIFIED | README and [JUDGE_DEMO.md](./JUDGE_DEMO.md) use `--db PATH`, supported legacy path-first forms, batch text syntax and JSONL/Base64 SERVE. |
| COMPACT command | VERIFIED | `python -m src.cli compact --db PATH`; engine compaction tests validate data and replacement behavior. |
| Max-bytes usage explained correctly | VERIFIED | Documentation distinguishes CLI `open --max-bytes` (that instance only) from passing `max_bytes` to the Database API used for writes. |

## Crash, scenario and torture verification

| Requirement | Status | Evidence / remaining limitation |
|---|---|---|
| Torture defaults to 500; seed required | VERIFIED | `torture.py` parser default and required `--seed`; 500-run result below. |
| Independent ACK oracle, durably separate | VERIFIED | `OracleLog.record` flushes and fsyncs events; `test_verifier_uses_fsynced_ack_events_from_oracle_log`; verifier tests reject lost/wrong ACKed state and accept legal unACKed outcomes. |
| Basic torture: 500 random kills/restarts | VERIFIED | Supplied result: seed `67890`, 500 PASS, 0 FAIL, 0 INCONCLUSIVE, 0 ERROR, elapsed 194.24 s. Raw transcript not checked in. |
| `large-value` mode | IMPLEMENTED BUT UNVERIFIED | Mode generates 1 MiB PUT/overwrite/delete and force-kills active SERVE; max five iterations. Exact A1-A5 boundary coverage is not proven. |
| `max-batch` mode | IMPLEMENTED BUT UNVERIFIED | Mode generates mixed/repeated-key batches of exactly 10,000 operations and reuses oracle verifier; 10,001 rejection is tested. Exact recovery-time target and all-or-none mid-batch kill need retained run evidence. |
| `recovery-crash` mode | VERIFIED | Current validation: seed `12345`, 3/3 PASS in 1.64 s. Each child paused inside `Database._recover` PUT replay, was force-killed after the marker, then fresh open/CHECK/DUMP/oracle verification passed. No standalone log file is checked in; no claim of repeated kills on the same recovery run. |
| `compaction-crash` mode | IMPLEMENTED BUT UNVERIFIED | Harness and scenario tests cover snapshot-write, pre-replace, and post-replace cases. No kill inside `os.replace` or power-loss validation. |
| `disk-full` mode | VERIFIED for injected ENOSPC only | Partial-write ENOSPC is injected; SERVE must return ERR, prior state is checked. It does not exhaust a physical disk or cover every hardware failure. |
| F1: 500-run invariant | VERIFIED | Same supplied 500/500 basic run and independent oracle result above. |
| F2: repeated recovery kills | IMPLEMENTED BUT UNVERIFIED | Isolated recovery-crash iterations pass, but repeated interruption during repair or multiple kills of the same database are not demonstrated. |
| F3: kill/reopen one million keys under 2 seconds | IMPLEMENTED BUT UNVERIFIED | Clean one-million-key reopen is measured at 0.809168 s; no kill during the million-key loading/recovery path was measured. |
| Complete A1-F4 proof matrix | IMPLEMENTED BUT UNVERIFIED | All 32 scenarios are mapped in ARCHITECTURE.md with exact expected outcomes, actual disk/recovery behavior, code, tests, and explicit status. Several required crash points remain incomplete. |

## Documentation, performance and submission

| Requirement | Status | Evidence / remaining limitation |
|---|---|---|
| README overview, setup, CLI, SERVE, storage, recovery, compaction, max-bytes, torture | VERIFIED | Updated `README.md`; examples follow inspected CLI/API syntax. |
| Architecture diagram, format, ordering, locking/recovery/compaction/disk-full, three guarantee arguments | VERIFIED | `ARCHITECTURE.md` plus source diagram `diagrams/aid_map_architecture.mmd`; claims explicitly bounded. |
| BREAKING within two printed pages | IMPLEMENTED BUT UNVERIFIED | Condensed five-section `BREAKING.md`; printer/font/pagination not validated in this environment. |
| TTL design answer | VERIFIED as proposal only | Proposed in BREAKING and JUDGE_DEMO; TTL is not implemented. |
| Offline, practical 15-minute judge guide | VERIFIED | `JUDGE_DEMO.md`; commands use disposable temp paths and call out illustrative output. Expensive benchmarks are not part of live steps. |
| Required AI prompt history, chronological/original/unmodified, from all teammates | MISSING | `PROMPTS.md` is a collection template only; no original teammate contributions are present. Teammates must paste their complete original chronological prompts, tool/interface and approximate time. |
| Ignore generated benchmark databases, torture artifacts and Python caches | VERIFIED | `.gitignore` excludes `benchmark_*.db`, torture temp directories, Python bytecode/cache, `.kv`, lock and compaction temp files. Existing user files were not deleted. |
| Million-key benchmark | VERIFIED | 1M insertion 54.554 s; recovery 0.809168 s; all keys present and sampled values correct. |
| All-live compaction interpretation | VERIFIED | 31,001,800 → 40,000,000 bytes; no obsolete records existed, so snapshot grew and this is not the churn shrinkage test. Reopen 1.627460 s; all million keys present. |
| Churn compaction acceptance | VERIFIED | 1M puts + 1M deletes; zero live keys; 51,003,600 → 0 bytes; 100% reclaimed; 166.26 s; reopen passed. |
| No external runtime dependencies | VERIFIED | Implementation and harness use Python standard library. |
| Merge into `main` | BLOCKED pending team review | This task authorizes a push to `integration-final`, not an automatic main merge. Compare `origin/main` to the pushed integration commit, then open/review a PR or merge only after team approval. |

## Validation record

Supplied prior results: **111 tests, OK, 8.197 seconds**; basic torture
**500 PASS / 0 FAIL / 0 INCONCLUSIVE / 0 ERROR**, seed `67890`, 194.24 s.
Current isolated full-suite rerun: **111 tests, OK**, unittest-reported
14.590 s. Current required smokes: basic **5/5 PASS**, seed `12345`, 2.15 s;
recovery-crash **3/3 PASS**, seed `12345`, 1.64 s. The suite was run from a
temporary current directory because legacy discovery modules execute
import-time scripts that write/delete relative `.kv` files.
