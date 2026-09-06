"""
Transaction Manager  —  transaction/manager.py

MVCC-based transaction management for MiniDB with multi-session support
via threading.local(). Each thread automatically gets its own transaction
context — no session_id needed anywhere.

  - Each row carries _xmin (creating txn) and _xmax (deleting txn).
  - BEGIN takes a snapshot of which txns are active/committed.
  - SELECT uses the snapshot to determine row visibility.
  - COMMIT marks the txn as committed in the MVCCManager.
  - ROLLBACK undoes changes.

How thread-local works:
  Flask handles each HTTP request in a separate thread.
  threading.local() gives each thread its own _current variable.
  Thread 1: _local._current = Transaction(id=5)
  Thread 2: _local._current = Transaction(id=6)
  They don't interfere with each other.
"""

import threading
import time

from storage.mvcc import MVCCManager, Snapshot, TXN_AUTO_COMMIT

ACTIVE    = "ACTIVE"
COMMITTED = "COMMITTED"
ABORTED   = "ABORTED"


class Transaction:

    _id_lock = threading.Lock()

    def __init__(self, txn_id: int, snapshot: Snapshot):
        self.id         = txn_id
        self.state      : str        = ACTIVE
        self.operations : list[dict] = []
        self.started_at : float      = time.time()
        self.snapshot   : Snapshot    = snapshot
        self._savepoints: dict       = {}

    def log_op(self, op: str, table: str, detail: dict = None):
        self.operations.append({
            "op":    op,
            "table": table,
            "ts":    time.time(),
            **(detail or {}),
        })

    def __repr__(self):
        return (f"Transaction(id={self.id}, state={self.state}, "
                f"ops={len(self.operations)}, "
                f"savepoints={list(self._savepoints.keys())})")


class TransactionManager:

    def __init__(self, engine):
        self.engine    = engine
        self._lock     = threading.RLock()
        # Thread-local storage: each thread gets its own _current transaction
        self._local    = threading.local()
        self._history  : list[Transaction]  = []
        self._table_locks: dict[str, threading.RLock] = {}

        # MVCC manager — shared across all threads/transactions
        self.mvcc = MVCCManager()

    # ------------------------------------------------------------------ #
    # Thread-local current transaction
    # ------------------------------------------------------------------ #

    @property
    def _current(self) -> Transaction | None:
        return getattr(self._local, "_current", None)

    @_current.setter
    def _current(self, txn):
        self._local._current = txn

    # ------------------------------------------------------------------ #
    # Table locks
    # ------------------------------------------------------------------ #

    def table_lock(self, table_name: str) -> threading.RLock:
        with self._lock:
            if table_name not in self._table_locks:
                self._table_locks[table_name] = threading.RLock()
            return self._table_locks[table_name]

    def acquire(self, table_name: str):
        self.table_lock(table_name).acquire()

    def release(self, table_name: str):
        self.table_lock(table_name).release()

    # ------------------------------------------------------------------ #
    # BEGIN  (MVCC)
    # ------------------------------------------------------------------ #

    def begin(self) -> Transaction:
        with self._lock:
            if self._current and self._current.state == ACTIVE:
                raise RuntimeError("Transaction already active")

            txn_id   = self.mvcc.allocate_txn_id()
            snapshot = self.mvcc.snapshot()

            txn = Transaction(txn_id, snapshot)
            self._current = txn

            self.engine.wal.write("BEGIN", {"txn_id": txn.id}, txn_id=txn.id)
            return txn

    # ------------------------------------------------------------------ #
    # COMMIT  (MVCC)
    # ------------------------------------------------------------------ #

    def commit(self) -> Transaction:
        with self._lock:
            txn = self._active_txn()

            self.mvcc.commit(txn.id)

            txn.state       = COMMITTED
            txn._savepoints = {}

            self.engine._flush_all()
            self.engine.wal.write("COMMIT", {}, txn_id=txn.id)
            self.engine.wal.checkpoint()

            self._history.append(txn)
            self._current = None
            return txn

    # ------------------------------------------------------------------ #
    # ROLLBACK  (MVCC)
    # ------------------------------------------------------------------ #

    def rollback(self) -> Transaction:
        with self._lock:
            txn = self._active_txn()

            for table in self.engine.tables.values():
                table.rollback_txn(txn.id)

            self.mvcc.abort(txn.id)

            txn.state       = ABORTED
            txn._savepoints = {}

            self.engine.wal.write("ROLLBACK", {}, txn_id=txn.id)
            self._history.append(txn)
            self._current = None
            return txn

    # ------------------------------------------------------------------ #
    # Savepoints  (MVCC-compatible)
    # ------------------------------------------------------------------ #

    def savepoint(self, name: str):
        with self._lock:
            txn = self._active_txn()
            sp_state = {}
            for name_t, table in self.engine.tables.items():
                sp_state[name_t] = {
                    "row_count": len(table.rows),
                    "deleted_by_us": [
                        i for i, r in enumerate(table.rows)
                        if r.get("_xmax") == txn.id
                    ],
                }
            txn._savepoints[name] = sp_state
            self.engine.wal.write(
                "SAVEPOINT", {"name": name}, txn_id=txn.id
            )

    def rollback_to(self, name: str):
        with self._lock:
            txn = self._active_txn()
            if name not in txn._savepoints:
                raise RuntimeError(f"Savepoint '{name}' does not exist")

            sp_state = txn._savepoints[name]

            for table_name, table in self.engine.tables.items():
                if table_name not in sp_state:
                    continue
                state = sp_state[table_name]

                old_deleted = set(state["deleted_by_us"])
                for i, row in enumerate(table.rows):
                    if row.get("_xmax") == txn.id and i not in old_deleted:
                        row["_xmax"] = 0

                table.rows = [
                    r for i, r in enumerate(table.rows)
                    if not (i >= state["row_count"] and r.get("_xmin") == txn.id)
                ]

                for index in table.indexes.values():
                    index.build(table.rows)

            keys = list(txn._savepoints.keys())
            for k in keys[keys.index(name) + 1:]:
                del txn._savepoints[k]

            self.engine.wal.write(
                "ROLLBACK_TO", {"name": name}, txn_id=txn.id
            )

    def release_savepoint(self, name: str):
        with self._lock:
            txn = self._active_txn()
            if name not in txn._savepoints:
                raise RuntimeError(f"Savepoint '{name}' does not exist")
            del txn._savepoints[name]
            self.engine.wal.write(
                "RELEASE_SAVEPOINT", {"name": name}, txn_id=txn.id
            )

    # ------------------------------------------------------------------ #
    # VACUUM
    # ------------------------------------------------------------------ #

    def vacuum(self) -> dict:
        with self._lock:
            oldest = self.mvcc.oldest_active_txn_id()
            committed = self.mvcc._committed
            total_removed = 0

            for table in self.engine.tables.values():
                removed = table.vacuum(oldest, committed)
                total_removed += removed

            if total_removed > 0:
                self.engine._flush_all()

            return {"removed": total_removed}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _active_txn(self) -> Transaction:
        if not self._current or self._current.state != ACTIVE:
            raise RuntimeError("No active transaction")
        return self._current

    @property
    def active(self) -> bool:
        return self._current is not None and self._current.state == ACTIVE

    @property
    def current_txn_id(self) -> int:
        return self._current.id if self._current else TXN_AUTO_COMMIT

    @property
    def current_snapshot(self) -> Snapshot | None:
        if self._current and self._current.state == ACTIVE:
            return self._current.snapshot
        return None

    def history(self) -> list[Transaction]:
        return list(self._history)
