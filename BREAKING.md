# Crash Points and Failure Notes

This note describes crash windows in the current implementation, not a
guarantee against arbitrary hardware or filesystem failure. Function line
numbers refer to this source snapshot and may move as the code changes.

## Crash windows

| # | Crash point and source | Possible database bytes | Startup response |
|---:|---|---|---|
| 1 | Initialization, [`Database.__init__`](./src/database.py#L21) | Empty/new file or unchanged old log; sidecar may exist | Replays from start |
| 2 | Append descriptor opened, [`append_record`](./src/storage.py#L30) | Usually old log; creation may have happened | Replays old complete records |
| 3 | Partial record header, [`append_record`](./src/storage.py#L40) | Incomplete trailing header | Truncates to record start and fsyncs |
| 4 | Partial record payload, [`append_record`](./src/storage.py#L40) | Complete header, short payload | Truncates whole record and fsyncs |
| 5 | Full record written, before/during fsync, [`append_record`](./src/storage.py#L45) | Incomplete tail or complete record; durability unknown | Repairs incomplete tail; replays complete valid record, even if unACKed |
| 6 | Fsync returned, before PUT index update, [`Database.put`](./src/database.py#L296) | Complete PUT record | Replays PUT |
| 7 | Complete BATCH append/fsync, before memory apply, [`Database.batch`](./src/database.py#L420) | One complete BATCH record | Replays every operation in that record |
| 8 | After method return, before response flush, [`serve`](./src/cli.py#L293) [`serve`](./src/cli.py#L294) | For non-empty write, fsynced record | Replays it; client may have no ACK and must treat outcome as unknown |
| 9 | During incomplete-tail repair, [`_read_records`](./src/storage.py#L51) | Truncate/fsync may be in progress | Later open retries; exact power-loss intermediate state is untested |
| 10 | Strict CHECK, [`verify_records`](./src/storage.py#L109) and [`Database.check_file`](./src/database.py#L344) | Data file opened read-only | Reports incomplete/corrupt record; does not repair |

DELETE shares the append windows above when the key exists. Deleting a missing
key returns `False` without appending; SERVE still ACKs the successful method
return. This is a no-op, not a durable delete record.

`FileLock` serializes operations across processes by locking the sidecar;
the per-instance `threading.RLock` serializes threads using one object.
Lock-release cleanup on abrupt process termination is delegated to the OS.
There is no compaction, rename, or file-replacement crash window because no
such implementation was found.

## Ordering argument

For a successful non-empty write: encode/validate -> acquire locks -> recover
and refresh -> append the encoded record (looped `os.write`) -> `os.fsync` the
database file -> update the in-memory index -> return/release locks -> write
the SERVE ACK line -> flush stdout. BATCH uses one BATCH record and applies
members to memory only after the record append/fsync returns.

An unACKed request is not necessarily rolled back: a full record may be
visible or durable even if the process dies before the caller sees a response.
Conversely, an ACKed write is expected to have returned from the code's
`os.fsync` call before ACK generation. These statements do not prove the
behavior of every storage device, filesystem, or power-loss scenario.

## Known weaknesses

1. There is no directory fsync after creation of a new database or lock
   sidecar, so persistence of the directory entry across sudden power loss is
   not established.
2. No configurable file-size cap, disk-full management policy, compaction, or
   bounded log-growth mechanism exists.
3. Recovery is a linear replay; there is no verified one-million-key latency
   or memory benchmark in this repository.
4. The record CRC excludes magic and payload-length fields. Strict validation
   catches many malformed records, but this is not a cryptographic integrity
   scheme or general corruption-repair mechanism.
5. CHECK requires an existing lock sidecar and validates the data without
   repairing it; an absent sidecar yields an error rather than a read-only
   unlocked scan.

## Observed failed attempts

- **Harness infrastructure failure, not a confirmed database failure:** a
  prior Windows smoke run encountered `OSError: [Errno 22] Invalid argument`
  while a writer thread was still using the SERVE stdin pipe as cleanup closed
  it. The harness cleanup was changed to stop/join writers before closing
  stdin. The exact command and seed were not retained in the evidence available
  for this document pass. This observed failure does not show a lost write or
  an engine correctness defect.
- No confirmed crash-induced engine correctness failure is documented in the
  evidence available for this document pass. Do not treat absence of a report
  as proof that no defect exists.

## Priorities for the next 24 hours

1. Add and test directory durability/creation semantics on the actual judging
   filesystem; document a chosen platform contract.
2. Implement a bounded-size policy with fault-injection tests for ENOSPC, or
   explicitly remove size-cap claims from the judging rubric.
3. Define compaction and recovery performance requirements, then add a
   separately measured compaction implementation and one-million-key benchmark
   only if required by the official specification.

## Team input needed

Paste the official crash-point/scenario text and exact expected outcomes here
or provide the official document. Also provide any failed test logs not
represented by the repository. No failure narrative should be added without
the command, environment, and observed result.
