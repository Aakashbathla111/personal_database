"""
Storage Engine  —  storage/engine.py

Central coordinator that owns all tables, the WAL, the buffer pool,
the transaction manager, and per-table concurrency locks.

MVCC Integration (Mod 10)
─────────────────────────
Every row now carries _xmin and _xmax transaction IDs.  The engine
passes the current txn_id and snapshot to all Table operations so that
visibility filtering is applied correctly.

Every public method that mutates data follows this pattern:
    1. Acquire table lock  (Phase 11 — concurrency)
    2. Write to WAL first  (Phase 9  — durability)
    3. Apply change in memory (with MVCC stamps)
    4. Persist to disk (or let buffer pool handle it)
    5. Release table lock
"""

import os
import json
import threading

from storage.column       import Column
from storage.table        import Table, _user_row
from storage.index        import Index
from storage.wal          import WAL
from storage.mvcc         import TXN_AUTO_COMMIT, Snapshot

TABLE_DIR = "data/tables"
VIEW_DIR  = "data/views"


class StorageEngine:

    def __init__(self):
        os.makedirs(TABLE_DIR, exist_ok=True)
        os.makedirs(VIEW_DIR,  exist_ok=True)

        self.tables     : dict[str, Table]  = {}
        self.views      : dict[str, dict]   = {}
        self.wal        = WAL()

        # Transaction manager is imported lazily to avoid circular import
        from transaction.manager import TransactionManager
        self.txn_mgr = TransactionManager(self)

        # Per-table RLocks for concurrent access (Phase 11)
        self._table_locks: dict[str, threading.RLock] = {}
        self._engine_lock = threading.RLock()

        # Row-level locks for UPDATE/DELETE — key is (table_name, row_index)
        self._row_locks: dict[tuple, threading.RLock] = {}
        self._row_lock_guard = threading.RLock()  # protects _row_locks dict itself

        self._load_tables()
        self._load_views()

    # ------------------------------------------------------------------ #
    # MVCC helpers — expose current txn state to Table layer
    # ------------------------------------------------------------------ #

    @property
    def current_txn_id(self) -> int:
        return self.txn_mgr.current_txn_id

    @property
    def current_snapshot(self) -> Snapshot | None:
        return self.txn_mgr.current_snapshot

    # ------------------------------------------------------------------ #
    # Concurrency helpers
    # ------------------------------------------------------------------ #

    def _lock(self, table_name: str) -> threading.RLock:
        with self._engine_lock:
            if table_name not in self._table_locks:
                self._table_locks[table_name] = threading.RLock()
            return self._table_locks[table_name]

    def _row_lock(self, table_name: str, row_idx: int) -> threading.RLock:
        """Get or create a lock for a specific row."""
        key = (table_name, row_idx)
        with self._row_lock_guard:
            if key not in self._row_locks:
                self._row_locks[key] = threading.RLock()
            return self._row_locks[key]

    # ------------------------------------------------------------------ #
    # Persistence helpers
    # ------------------------------------------------------------------ #

    def _table_path(self, name): return f"{TABLE_DIR}/{name}.json"
    def _view_path(self,  name): return f"{VIEW_DIR}/{name}.json"

    def _save_table(self, table: Table):
        payload = {
            "name":    table.name,
            "columns": [c.to_dict() for c in table.columns],
            "rows":    table.rows,   # includes _xmin/_xmax for MVCC
            "indexes": [idx.to_dict() for idx in table.indexes.values()],
        }
        with open(self._table_path(table.name), "w") as f:
            json.dump(payload, f, indent=2, default=str)

    def _flush_all(self):
        for table in self.tables.values():
            self._save_table(table)

    def _load_tables(self):
        for fname in os.listdir(TABLE_DIR):
            if not fname.endswith(".json"):
                continue
            with open(f"{TABLE_DIR}/{fname}") as f:
                payload = json.load(f)
            columns = [Column.from_dict(c) for c in payload["columns"]]
            table   = Table(payload["name"], columns)
            table.rows = payload.get("rows", [])
            # Ensure MVCC metadata exists on loaded rows
            for row in table.rows:
                if "_xmin" not in row:
                    row["_xmin"] = TXN_AUTO_COMMIT
                if "_xmax" not in row:
                    row["_xmax"] = 0
            for idx_data in payload.get("indexes", []):
                idx = Index.from_dict(idx_data)
                idx.build(table.rows)
                table.indexes[idx.name] = idx
            self.tables[table.name] = table

            # Track max txn_id from loaded rows to avoid ID collisions
            for row in table.rows:
                xmin = row.get("_xmin", 0)
                xmax = row.get("_xmax", 0)
                max_id = max(xmin, xmax)
                if max_id >= self.txn_mgr.mvcc.next_txn_id:
                    self.txn_mgr.mvcc.next_txn_id = max_id + 1
                # Mark non-zero, non-auto-commit xmin as committed
                # (they were persisted, so they must have been committed)
                if xmin > 0:
                    self.txn_mgr.mvcc._committed.add(xmin)

    def _load_views(self):
        for fname in os.listdir(VIEW_DIR):
            if not fname.endswith(".json"):
                continue
            with open(f"{VIEW_DIR}/{fname}") as f:
                payload = json.load(f)
            self.views[payload["name"]] = payload

    # ------------------------------------------------------------------ #
    # CREATE TABLE
    # ------------------------------------------------------------------ #

    def create_table(self, name: str, columns: list):
        with self._engine_lock:
            if name in self.tables:
                raise ValueError(f"Table '{name}' already exists")
            table = Table(name, columns)
            self.tables[name] = table
            self._table_locks[name] = threading.RLock()
            self.wal.write("CREATE_TABLE", {"table": name},
                           txn_id=self.current_txn_id)
            self._save_table(table)
            return table

    # ------------------------------------------------------------------ #
    # DROP TABLE
    # ------------------------------------------------------------------ #

    def drop_table(self, name: str, if_exists=False):
        with self._engine_lock:
            if name not in self.tables:
                if if_exists:
                    return
                raise KeyError(f"Table '{name}' not found")
            del self.tables[name]
            self._table_locks.pop(name, None)
            path = self._table_path(name)
            if os.path.exists(path):
                os.remove(path)

    # ------------------------------------------------------------------ #
    # TRUNCATE
    # ------------------------------------------------------------------ #

    def truncate(self, name: str) -> int:
        with self._lock(name):
            table = self.get_table(name)
            count = table.truncate()
            self._save_table(table)
            return count

    # ------------------------------------------------------------------ #
    # Auto-commit helper
    # ------------------------------------------------------------------ #

    def _auto_commit_txn_id(self) -> int:
        """
        For auto-commit mode, allocate a real txn_id and immediately
        mark it committed so MVCC visibility works correctly.
        """
        txn_id = self.txn_mgr.mvcc.allocate_txn_id()
        self.txn_mgr.mvcc.commit(txn_id)
        return txn_id

    # ------------------------------------------------------------------ #
    # INSERT  (MVCC)
    # ------------------------------------------------------------------ #

    def insert(self, table_name: str, values: list):
        with self._lock(table_name):
            table = self.get_table(table_name)

            if self.txn_mgr.active:
                txn_id = self.current_txn_id
                snap   = self.current_snapshot
            else:
                txn_id = self._auto_commit_txn_id()
                snap   = self.txn_mgr.mvcc.snapshot()

            row = table.insert(values, engine=self, txn_id=txn_id,
                               snapshot=snap)

            self.wal.write("INSERT", {"table": table_name,
                                      "row": _user_row(row)},
                           txn_id=txn_id)
            if self.txn_mgr.active:
                self.txn_mgr._current.log_op("INSERT", table_name,
                                             {"row": _user_row(row)})
            else:
                self._save_table(table)
            return _user_row(row)

    def insert_named(self, table_name: str, data: dict):
        with self._lock(table_name):
            table = self.get_table(table_name)

            if self.txn_mgr.active:
                txn_id = self.current_txn_id
                snap   = self.current_snapshot
            else:
                txn_id = self._auto_commit_txn_id()
                snap   = self.txn_mgr.mvcc.snapshot()

            row = table.insert_dict(data, engine=self, txn_id=txn_id,
                                    snapshot=snap)

            self.wal.write("INSERT", {"table": table_name,
                                      "row": _user_row(row)},
                           txn_id=txn_id)
            if self.txn_mgr.active:
                self.txn_mgr._current.log_op("INSERT", table_name,
                                             {"row": _user_row(row)})
            else:
                self._save_table(table)
            return _user_row(row)

    # ------------------------------------------------------------------ #
    # SELECT  (MVCC)
    # ------------------------------------------------------------------ #

    def select(self, table_name: str, columns="*", where=None,
               order_by=None, order_dir="ASC", limit=None, offset=0,
               group_by=None, having=None):
        # MVCC ensures read consistency — no lock needed for SELECT
        if table_name in self.views:
            return self._query_view(table_name, where, columns,
                                    order_by, order_dir, limit, offset)
        table = self.get_table(table_name)

        if self.txn_mgr.active:
            txn_id = self.current_txn_id
            snap   = self.current_snapshot
        else:
            txn_id = TXN_AUTO_COMMIT
            snap   = self.txn_mgr.mvcc.snapshot()
            print(f"snapshot for auto-commit: {snap.active_at_start}, {snap.committed}, {snap.max_txn_id}")

        return table.select(columns=columns, where=where,
                            order_by=order_by, order_dir=order_dir,
                            limit=limit, offset=offset,
                            group_by=group_by, having=having,
                            txn_id=txn_id, snapshot=snap)

    def _query_view(self, view_name, where, columns,
                    order_by, order_dir, limit, offset):
        view        = self.views[view_name]
        base        = view["base_table"]
        view_where  = view.get("where")
        combined    = (f"({view_where}) AND ({where})"
                       if view_where and where
                       else view_where or where)
        return self.select(base,
                           columns=columns or view.get("columns", "*"),
                           where=combined,
                           order_by=order_by, order_dir=order_dir,
                           limit=limit, offset=offset)

    # ------------------------------------------------------------------ #
    # UPDATE  (MVCC)
    # ------------------------------------------------------------------ #

    def update(self, table_name: str, assignments: dict, where=None) -> int:
        table = self.get_table(table_name)

        if self.txn_mgr.active:
            txn_id = self.current_txn_id
            snap   = self.current_snapshot
        else:
            txn_id = self._auto_commit_txn_id()
            snap   = self.txn_mgr.mvcc.snapshot()

        from storage.table import _eval_where, _user_row
        from storage.mvcc import row_is_visible
        target_indices = []
        for i, row in enumerate(table.rows):
            if row.get("_xmax", 0) != 0:
                if snap and not row_is_visible(row, txn_id, snap):
                    continue
                elif not snap:
                    continue
            if snap and not row_is_visible(row, txn_id, snap):
                continue
            if _eval_where(_user_row(row), where):
                target_indices.append(i)

        if not target_indices:
            return 0

        row_locks = [self._row_lock(table_name, i) for i in sorted(target_indices)]
        for rl in row_locks:
            rl.acquire()
        try:
            count = table.update(assignments, where=where, engine=self,
                                 txn_id=txn_id, snapshot=snap)

            self.wal.write("UPDATE",
                           {"table": table_name, "set": assignments,
                            "where": where},
                           txn_id=txn_id)
            if self.txn_mgr.active:
                self.txn_mgr._current.log_op("UPDATE", table_name,
                                             {"set": assignments,
                                              "where": where})
            else:
                self._save_table(table)
            return count
        finally:
            for rl in row_locks:
                rl.release()

    # ------------------------------------------------------------------ #
    # DELETE  (MVCC)
    # ------------------------------------------------------------------ #

    def delete(self, table_name: str, where=None) -> int:
        table = self.get_table(table_name)

        if self.txn_mgr.active:
            txn_id = self.current_txn_id
            snap   = self.current_snapshot
        else:
            txn_id = self._auto_commit_txn_id()
            snap   = self.txn_mgr.mvcc.snapshot()

        from storage.table import _eval_where, _user_row
        from storage.mvcc import row_is_visible
        target_indices = []
        for i, row in enumerate(table.rows):
            if row.get("_xmax", 0) != 0:
                if snap and not row_is_visible(row, txn_id, snap):
                    continue
                elif not snap:
                    continue
            if snap and not row_is_visible(row, txn_id, snap):
                continue
            if _eval_where(_user_row(row), where):
                target_indices.append(i)

        if not target_indices:
            return 0

        row_locks = [self._row_lock(table_name, i) for i in sorted(target_indices)]
        for rl in row_locks:
            rl.acquire()
        try:
            count = table.delete(where=where, txn_id=txn_id, snapshot=snap)

            self.wal.write("DELETE",
                           {"table": table_name, "where": where},
                           txn_id=txn_id)
            if self.txn_mgr.active:
                self.txn_mgr._current.log_op("DELETE", table_name,
                                             {"where": where})
            else:
                self._save_table(table)
            return count
        finally:
            for rl in row_locks:
                rl.release()

    # ------------------------------------------------------------------ #
    # ALTER TABLE
    # ------------------------------------------------------------------ #

    def alter_add_column(self, table_name: str, column: Column):
        with self._lock(table_name):
            self.get_table(table_name).add_column(column)
            self._save_table(self.get_table(table_name))

    def alter_drop_column(self, table_name: str, col_name: str):
        with self._lock(table_name):
            self.get_table(table_name).drop_column(col_name)
            self._save_table(self.get_table(table_name))

    def alter_rename_column(self, table_name: str, old: str, new: str):
        with self._lock(table_name):
            self.get_table(table_name).rename_column(old, new)
            self._save_table(self.get_table(table_name))

    def rename_table(self, old_name: str, new_name: str):
        with self._engine_lock:
            if old_name not in self.tables:
                raise KeyError(f"Table '{old_name}' not found")
            if new_name in self.tables:
                raise ValueError(f"Table '{new_name}' already exists")
            table      = self.tables.pop(old_name)
            table.name = new_name
            self.tables[new_name] = table
            old_path = self._table_path(old_name)
            if os.path.exists(old_path):
                os.remove(old_path)
            self._save_table(table)

    # ------------------------------------------------------------------ #
    # INDEXES
    # ------------------------------------------------------------------ #

    def create_index(self, index_name: str, table_name: str,
                     col_name: str, unique=False):
        with self._lock(table_name):
            table = self.get_table(table_name)
            idx   = table.create_index(index_name, col_name, unique)
            self._save_table(table)
            return idx

    def drop_index(self, index_name: str, table_name: str):
        with self._lock(table_name):
            self.get_table(table_name).drop_index(index_name)
            self._save_table(self.get_table(table_name))

    def show_indexes(self, table_name: str):
        table = self.get_table(table_name)
        return [{"Index": k,
                 "Column": ", ".join(v.column_name) if isinstance(v.column_name, list) else v.column_name,
                 "Unique": v.unique,
                 "Composite": v.is_composite}
                for k, v in table.indexes.items()]

    # ------------------------------------------------------------------ #
    # VIEWS
    # ------------------------------------------------------------------ #

    def create_view(self, view_name: str, base_table: str,
                    columns="*", where=None):
        if view_name in self.views:
            raise ValueError(f"View '{view_name}' already exists")
        view = {"name": view_name, "base_table": base_table,
                "columns": columns, "where": where}
        self.views[view_name] = view
        with open(self._view_path(view_name), "w") as f:
            json.dump(view, f, indent=2)

    def drop_view(self, view_name: str, if_exists=False):
        if view_name not in self.views:
            if if_exists:
                return
            raise KeyError(f"View '{view_name}' not found")
        del self.views[view_name]
        path = self._view_path(view_name)
        if os.path.exists(path):
            os.remove(path)

    # ------------------------------------------------------------------ #
    # JOIN  (MVCC)
    # ------------------------------------------------------------------ #

    def join(self, left_table: str, right_table: str,
             on_left: str, on_right: str, join_type="INNER",
             where=None, columns="*", order_by=None,
             order_dir="ASC", limit=None):
        from storage.table import _eval_where
        txn_id = self.current_txn_id
        snap   = self.current_snapshot

        # MVCC ensures read consistency — no lock needed for JOIN (read-only)
        left  = self.get_table(left_table)
        right = self.get_table(right_table)
        rows  = left.join(right, on_left, on_right, join_type,
                          txn_id=txn_id, snapshot=snap)

        if where:
            rows = [r for r in rows if _eval_where(r, where)]
        if order_by:
            reverse = order_dir.upper() == "DESC"
            rows = sorted(rows,
                          key=lambda r: (r.get(order_by) is None,
                                         r.get(order_by)),
                          reverse=reverse)
        if limit is not None:
            rows = rows[:limit]
        if columns != "*":
            col_list = [c.strip() for c in columns.split(",")]
            rows = [{c: r.get(c) for c in col_list} for r in rows]
        return rows

    # ------------------------------------------------------------------ #
    # TRANSACTIONS  (delegates to TransactionManager)
    # ------------------------------------------------------------------ #

    def begin(self):
        self.txn_mgr.begin()

    def commit(self):
        self.txn_mgr.commit()

    def rollback(self):
        self.txn_mgr.rollback()

    # ------------------------------------------------------------------ #
    # VACUUM
    # ------------------------------------------------------------------ #

    def vacuum(self) -> dict:
        """Remove dead MVCC row versions across all tables."""
        return self.txn_mgr.vacuum()

    # ------------------------------------------------------------------ #
    # INFO
    # ------------------------------------------------------------------ #

    def describe(self, table_name: str):
        return self.get_table(table_name).describe()

    def show_tables(self):
        return sorted(self.tables.keys())

    def show_views(self):
        return sorted(self.views.keys())

    def get_table(self, name: str) -> Table:
        if name not in self.tables:
            raise KeyError(f"Table '{name}' not found")
        return self.tables[name]

    # ------------------------------------------------------------------ #
    # WAL RECOVERY
    # ------------------------------------------------------------------ #

    def recover(self):
        """
        WAL-based crash recovery — Mod 2.

        Algorithm (REDO-only):
          1. Read all WAL records in LSN order.
          2. Collect the set of committed txn_ids from COMMIT records.
          3. Replay INSERT / UPDATE / DELETE for:
               • auto-commit operations  (txn_id == 0)
               • any txn whose id appears in the committed set
          4. Skip records from in-flight (uncommitted) transactions.
          5. Flush tables to disk and truncate the WAL (checkpoint).

        This satisfies the Durability and Atomicity guarantees of ACID:
          - Durability  : committed data survives a crash.
          - Atomicity   : partial (uncommitted) writes are not replayed.
        """
        records   = self.wal.read_all()
        committed = self.wal.committed_txn_ids()

        if not records:
            print("Nothing to recover.")
            return

        print(f"Replaying {len(records)} WAL record(s) "
              f"(committed txns: {committed or 'none'}) ...")

        replayed = 0
        skipped  = 0

        for rec in records:
            op     = rec.get("op")
            txn_id = rec.get("txn_id", 0)
            lsn    = rec.get("lsn")

            # Skip control records
            if op in ("BEGIN", "COMMIT", "ROLLBACK", "CREATE_TABLE"):
                continue

            # Only replay committed or auto-commit operations
            if txn_id != 0 and txn_id not in committed:
                skipped += 1
                continue

            try:
                if op == "INSERT":
                    table = self.get_table(rec["table"])
                    row   = rec["row"]
                    # Check if row already exists (avoid duplicates)
                    exists = any(
                        all(existing.get(k) == v for k, v in row.items()
                            if k not in ("_xmin", "_xmax"))
                        for existing in table.visible_rows()
                    )
                    if not exists:
                        from storage.mvcc import stamp_insert
                        stamp_insert(row, TXN_AUTO_COMMIT)
                        row["_xmax"] = 0
                        table.rows.append(row)
                        for idx in table.indexes.values():
                            idx.build(table.rows)
                        replayed += 1

                elif op == "UPDATE":
                    table = self.get_table(rec["table"])
                    table.update(rec.get("set", {}), where=rec.get("where"))
                    replayed += 1

                elif op == "DELETE":
                    table = self.get_table(rec["table"])
                    table.delete(where=rec.get("where"))
                    replayed += 1

            except Exception as e:
                print(f"  Skip record LSN={lsn}: {e}")

        self._flush_all()
        self.wal.checkpoint()
        print(f"Recovery complete: {replayed} record(s) replayed, "
              f"{skipped} uncommitted record(s) skipped.")
