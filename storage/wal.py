"""
Write-Ahead Log  —  storage/wal.py

Every mutating operation is written to the WAL *before* it is applied
to the table — this guarantees Atomicity and Durability (two of ACID).

WAL record format (one JSON line per record)
────────────────────────────────────────────
{
  "lsn":   101,           ← Log Sequence Number (monotonically increasing)
  "ts":    1718000000.0,  ← Unix timestamp
  "txn_id": 3,            ← which transaction (0 = auto-commit)
  "op":    "INSERT",
  "table": "users",
  "row":   {"id": 1, "name": "Alice"}
}

Recovery algorithm
──────────────────
1. Read all WAL records in LSN order.
2. Re-apply any INSERT / UPDATE / DELETE that belongs to a
   COMMITTED transaction (or auto-commit txn_id=0).
3. Ignore records whose txn_id is not in the committed set
   (i.e. the transaction was in-flight when the crash happened).
4. Call checkpoint() to clear the WAL after recovery.
"""

import os
import json
import time

WAL_FILE = "data/wal.log"


class WAL:

    def __init__(self):
        os.makedirs("data", exist_ok=True)
        self._lsn = self._last_lsn() + 1

    # ------------------------------------------------------------------ #
    # LSN management
    # ------------------------------------------------------------------ #

    def _last_lsn(self) -> int:
        if not os.path.exists(WAL_FILE):
            return 0
        last = 0
        with open(WAL_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line).get("lsn", last)
                    except json.JSONDecodeError:
                        pass
        return last

    def _next_lsn(self) -> int:
        lsn = self._lsn
        self._lsn += 1
        return lsn

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def write(self, operation: str, payload: dict, txn_id: int = 0):
        """
        Append one WAL record.

        Parameters
        ----------
        operation : "INSERT" | "UPDATE" | "DELETE" | "BEGIN" | "COMMIT" | "ROLLBACK"
        payload   : e.g. {"table": "users", "row": {...}}
        txn_id    : transaction id (0 = auto-commit)
        """
        record = {
            "lsn":    self._next_lsn(),
            "ts":     time.time(),
            "txn_id": txn_id,
            "op":     operation,
            **payload,
        }
        with open(WAL_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return record["lsn"]

    # ------------------------------------------------------------------ #
    # Read / Recovery
    # ------------------------------------------------------------------ #

    def read_all(self) -> list[dict]:
        if not os.path.exists(WAL_FILE):
            return []
        records = []
        with open(WAL_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return sorted(records, key=lambda r: r["lsn"])

    def committed_txn_ids(self) -> set[int]:
        """Return the set of txn_ids that have a COMMIT record."""
        committed = set()
        for rec in self.read_all():
            if rec["op"] == "COMMIT":
                committed.add(rec["txn_id"])
        return committed

    # ------------------------------------------------------------------ #
    # Checkpoint
    # ------------------------------------------------------------------ #

    def checkpoint(self):
        """Truncate the WAL after a successful flush — marks a safe point."""
        if os.path.exists(WAL_FILE):
            open(WAL_FILE, "w").close()
        self._lsn = 1     # reset LSN counter

    def clear(self):
        self.checkpoint()
