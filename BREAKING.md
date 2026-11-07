# BREAKING: Aid_Map Crash Windows

This note describes code paths and process-kill evidence, not a guarantee
against power loss or every storage device. References point to the current
source; line numbers may change.

## 1. Crash windows and recovery

| Crash point | File state | Recovery behavior |
|---|---|---|
| Before first append byte (`append_record`, `src/storage.py`) | No bytes for the new record; previous log remains. | Replay retains previous value/state. A kill exactly at this boundary is not independently tested. |
| During append (`append_record`) | Incomplete header/payload may remain after force-kill. | `_read_records` truncates to the record start, flushes, and fsyncs. A caught partial-write exception instead attempts immediate truncate+fsync rollback. |
| Full record before/during fsync (`append_record`) | Complete record can be visible even when caller received no ACK; persistence before fsync is uncertain. | A complete CRC-valid record is replayed; a torn one is removed. No ACK means the client must treat the operation as unknown. |
| After fsync but before ACK (`Database.put` / `Database.batch`, then `serve`) | Complete record has passed file fsync, but output may not have been observed. | Reopen replays it. The oracle permits an eligible unACKed request to have committed or not. |
| Batch append before return (`Database.batch`) | One BATCH record, not a sequence of independently visible members. | A torn record is discarded as a unit; a complete valid record is applied in order. There is no separate commit-marker record. |
| Compaction before `os.replace` (`replace_records_atomically`, `src/storage.py`) | Original database path remains authoritative; temp snapshot may exist. | Recovery opens the original. Compaction kill tests cover snapshot/pre-replacement cases. |
| Compaction after `os.replace`, before cleanup (`replace_records_atomically`) | New complete snapshot is at the database path; the former temp name has moved. | Recovery opens the replacement. On non-Windows, directory fsync follows; Windows has no equivalent directory fsync here. |

`Database._recover` rebuilds only an in-memory index. The
`recovery-crash` mode pauses within record replay, force-kills the child, then
reopens and verifies using CHECK, DUMP, and the independent fsynced ACK
oracle. The three-iteration seeded smoke passed. It does not emulate
power-loss interruption during the repair truncate/fsync itself.

## 2. Persistence and atomicity argument

The ordering is: validate/encode -> acquire `threading.RLock` and sidecar
`FileLock` -> refresh/recover -> append full record -> `os.fsync(database
fd)` -> update memory -> return/release locks -> SERVE writes and flushes ACK.
Compaction writes and fsyncs the temporary snapshot before `os.replace`; on
POSIX it then fsyncs the parent directory. The one-record BATCH format gives
whole-record replay for a torn tail, but does not implement the official
separate commit-marker boundary in B2.

## 3. Known weaknesses and limits

- Database/lock-file creation is not followed by directory fsync. Compaction
  also lacks directory fsync on Windows, so sudden power-loss durability of
  directory entries/replacement is not established.
- Force-kill tests terminate processes; they do not simulate power failure,
  volatile device caches, or all NTFS/filesystem failure modes.
- A one-byte-short final record causes recovery to discard that whole record,
  which could be the last ACKed operation; the official F4 guarantee is not
  established and may conflict with current tail repair.
- There is no separate batch commit marker. B2's stated visibility boundary
  is therefore not represented.
- `max_bytes` applies to append operations on an explicitly configured
  `Database` instance, is not persisted, and does not cap compaction's
  temporary snapshot. Rollback failure is reported but can leave repair work
  for the next open.
- Benchmark transcripts and complete original AI prompt histories are not
  checked into the repository.

## 4. Prioritized improvements for 24 hours

1. Define the judging durability contract and implement/test the required
   directory durability path on Windows; add power-cut or VM-reset evidence
   if the rubric requires power-loss claims.
2. Resolve the batch-marker and F4 acceptance mismatches explicitly. Add
   deterministic hooks/tests for every single-write boundary, torn-tail
   repair, compaction replacement, and long-batch concurrency.
3. Check in benchmark transcripts, run the complete mode matrix on the judge
   filesystem, and collect each teammate's original chronological prompts.

## 5. TTL design (proposed; not implemented)

Persist an absolute UTC expiration timestamp in a versioned PUT payload
(including each PUT member of a BATCH); do not persist a process-local
monotonic-clock value, which cannot be compared after restart. A read treats
the key as absent when `now_utc_ms >= expires_at_utc_ms`. Compaction omits
expired entries and emits ordinary live PUT snapshots; an explicit delete
still writes a tombstone. Define clock rollback as delaying expiration until
the wall clock reaches the stored deadline again, and test that contract.
This needs a format-version/migration decision before implementation.

## Observed failures

No confirmed crash-induced data-loss failure is recorded in this submission.
An earlier Windows harness cleanup reported `OSError: [Errno 22] Invalid
argument` while a writer still used the SERVE pipe; cleanup ordering was
adjusted to stop/join writers before closing streams. The exact command and
seed were not retained, so this is infrastructure history, not evidence of
an engine defect.
