"""
MVCC (Multi-Version Concurrency Control)  —  storage/mvcc.py

PostgreSQL-style MVCC implementation for MiniDB.

How it works
────────────
Every row carries two hidden system columns:

  _xmin  — the transaction ID that **created** this row version.
  _xmax  — the transaction ID that **deleted** (or replaced) this row version.
            0 means "not deleted" (the row is still live).

Visibility rule
───────────────
A row version is visible to transaction T if:

  1. _xmin is committed (or _xmin == T, i.e. "I created it")
  AND
  2. _xmax == 0                          (not deleted yet)
     OR _xmax is NOT committed           (deleter hasn't committed)
     AND _xmax != T                      (I didn't delete it myself)

When _xmax != 0 AND _xmax is committed, the row is "dead" — invisible to
all future transactions and eligible for VACUUM cleanup.

Operations
──────────
  INSERT  → append row with _xmin = current_txn_id, _xmax = 0
  DELETE  → set _xmax = current_txn_id  (row stays in storage)
  UPDATE  → DELETE old row + INSERT new row  (two versions)
  VACUUM  → physically remove rows where _xmax is committed
            and no active transaction can see them.

Transaction lifecycle
─────────────────────
  BEGIN    → allocate new txn_id, record snapshot of active txn set
  COMMIT   → add txn_id to committed set
  ROLLBACK → mark all rows with _xmin == txn_id as dead (_xmax = txn_id)
             and restore rows with _xmax == txn_id (_xmax = 0)
"""

import threading

# Special transaction IDs
TXN_AUTO_COMMIT = 0   # auto-commit operations are immediately visible


class MVCCManager:
    """Tracks transaction states for MVCC visibility decisions."""

    def __init__(self):
        self._lock = threading.Lock()

        # Monotonically increasing transaction ID counter
        self._next_txn_id = 1

        # Set of committed transaction IDs
        self._committed: set[int] = set()

        # Set of aborted (rolled-back) transaction IDs
        self._aborted: set[int] = set()

        # Set of currently active transaction IDs
        self._active: set[int] = set()

    def allocate_txn_id(self) -> int:
        """Allocate and return a new unique transaction ID."""
        with self._lock:
            txn_id = self._next_txn_id
            self._next_txn_id += 1
            self._active.add(txn_id)
            return txn_id

    def commit(self, txn_id: int):
        """Mark a transaction as committed."""
        with self._lock:
            self._active.discard(txn_id)
            self._committed.add(txn_id)

    def abort(self, txn_id: int):
        """Mark a transaction as aborted."""
        with self._lock:
            self._active.discard(txn_id)
            self._aborted.add(txn_id)

    def is_committed(self, txn_id: int) -> bool:
        """Check if a transaction has committed."""
        if txn_id == TXN_AUTO_COMMIT:
            return True  # auto-commit is always visible
        with self._lock:
            return txn_id in self._committed

    def is_aborted(self, txn_id: int) -> bool:
        """Check if a transaction has been aborted."""
        with self._lock:
            return txn_id in self._aborted

    def active_txn_ids(self) -> set[int]:
        """Return snapshot of currently active transaction IDs."""
        with self._lock:
            return set(self._active)

    def snapshot(self) -> "Snapshot":
        """
        Take a point-in-time snapshot for a transaction.
        The snapshot records which txns are active at this moment,
        so the transaction gets REPEATABLE READ isolation.
        """
        with self._lock:
            return Snapshot(
                active_at_start=set(self._active),
                committed=set(self._committed),
                max_txn_id=self._next_txn_id - 1,
            )

    def oldest_active_txn_id(self) -> int:
        """Return the smallest active txn_id, or current max+1 if none active."""
        with self._lock:
            if self._active:
                return min(self._active)
            return self._next_txn_id

    @property
    def next_txn_id(self) -> int:
        with self._lock:
            return self._next_txn_id

    @next_txn_id.setter
    def next_txn_id(self, val: int):
        with self._lock:
            self._next_txn_id = val


class Snapshot:
    """A point-in-time snapshot of transaction visibility state."""

    def __init__(self, active_at_start: set[int], committed: set[int],
                 max_txn_id: int):
        self.active_at_start = active_at_start
        self.committed = committed
        self.max_txn_id = max_txn_id

    def is_visible(self, xmin: int, xmax: int, my_txn_id: int) -> bool:
        """
        Determine if a row version (xmin, xmax) is visible to transaction
        my_txn_id using this snapshot.

        A row is visible if:
          1. xmin is "visible" (committed before snapshot, or is my own txn)
          2. xmax is "not visible" (0, not committed, or was active at snapshot time)
             unless xmax == my_txn_id (I deleted it myself — then it's invisible)
        """
        # Step 1: Is the row creator visible to me?
        if xmin == my_txn_id:
            # I created this row — visible unless I also deleted it
            if xmax == my_txn_id:
                return False  # I created and deleted it in same txn
            if xmax == 0:
                return True   # I created it, not deleted
            # Someone else set xmax, but hasn't committed — still visible to me
            return xmax not in self.committed

        if xmin == TXN_AUTO_COMMIT:
            xmin_visible = True
        elif xmin in self.active_at_start:
            xmin_visible = False  # creator was still active when I started
        elif xmin > self.max_txn_id:
            xmin_visible = False  # creator started after me
        elif xmin in self.committed:
            xmin_visible = True   # creator committed before my snapshot
        else:
            xmin_visible = False  # not committed = not visible

        if not xmin_visible:
            return False

        # Step 2: Is the row still alive?
        if xmax == 0:
            return True  # not deleted

        if xmax == my_txn_id:
            return False  # I deleted it

        if xmax == TXN_AUTO_COMMIT:
            return False  # deleted by auto-commit (immediately visible deletion)

        if xmax in self.active_at_start:
            return True  # deleter was active at my start — deletion not visible

        if xmax > self.max_txn_id:
            return True  # deleter started after me

        if xmax in self.committed:
            return False  # deleter committed before my snapshot

        return True  # deleter not committed — row still alive to me


def row_is_visible(row: dict, txn_id: int, snapshot: "Snapshot") -> bool:
    """
    Check if a row is visible to the given transaction.
    Rows without MVCC metadata (_xmin/_xmax) are treated as always-visible
    for backwards compatibility.
    """
    xmin = row.get("_xmin", TXN_AUTO_COMMIT)
    xmax = row.get("_xmax", 0)
    return snapshot.is_visible(xmin, xmax, txn_id)


def stamp_insert(row: dict, txn_id: int) -> dict:
    """Stamp a row with MVCC metadata on insert."""
    row["_xmin"] = txn_id
    row["_xmax"] = 0
    return row


def stamp_delete(row: dict, txn_id: int) -> dict:
    """Mark a row as deleted by setting _xmax."""
    row["_xmax"] = txn_id
    return row


def is_dead(row: dict, oldest_active: int, committed: set[int]) -> bool:
    """
    Check if a row version is dead (can be vacuumed).
    A row is dead if:
      - _xmax is set (> 0)
      - _xmax is committed
      - No active transaction could possibly see this row version
        (the deleter's txn_id < oldest_active_txn_id)
    """
    xmax = row.get("_xmax", 0)
    if xmax == 0:
        return False
    if xmax == TXN_AUTO_COMMIT:
        # Auto-commit deletions: check if creator is also done
        xmin = row.get("_xmin", TXN_AUTO_COMMIT)
        return xmin == TXN_AUTO_COMMIT or xmin in committed
    if xmax not in committed:
        return False
    # xmax is committed — dead if no active txn started before the delete
    return xmax < oldest_active
