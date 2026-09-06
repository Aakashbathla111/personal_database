import operator
import re
from storage.column import Column
from storage.index import Index
from storage.mvcc import (
    MVCCManager, Snapshot, TXN_AUTO_COMMIT,
    row_is_visible, stamp_insert, stamp_delete, is_dead,
)

_OPS = {
    "=":  operator.eq,
    "!=": operator.ne,
    "<>": operator.ne,
    "<":  operator.lt,
    "<=": operator.le,
    ">":  operator.gt,
    ">=": operator.ge,
}


def _coerce(a, b):
    """Try to compare a (stored) and b (string literal) with matching types."""
    if isinstance(a, bool):
        return a, b.upper() in ("TRUE", "1", "YES")
    try:
        if isinstance(a, int):
            return a, int(b)
        if isinstance(a, float):
            return a, float(b)
    except (ValueError, TypeError):
        pass
    return str(a) if a is not None else "", str(b)


def _eval_condition(row, condition):
    """Evaluate a single 'col OP value' or 'col IS NULL / IS NOT NULL'."""
    condition = condition.strip()
    # Normalise spaces around parentheses so that tokeniser output like
    # "AVG ( score )" is treated the same as "AVG(score)".
    condition = re.sub(r'\s*\(\s*', '(', condition)
    condition = re.sub(r'\s*\)\s*', ')', condition)

    # IS NULL / IS NOT NULL
    m = re.match(r"(\w+)\s+IS\s+(NOT\s+)?NULL", condition, re.IGNORECASE)
    if m:
        col, not_ = m.group(1), m.group(2)
        val = row.get(col)
        return (val is None) if not not_ else (val is not None)

    # LIKE
    m = re.match(r"(\w+)\s+LIKE\s+'([^']*)'", condition, re.IGNORECASE)
    if m:
        col, pattern = m.group(1), m.group(2)
        regex_parts = []
        for ch in pattern:
            if ch == "%":
                regex_parts.append(".*")
            elif ch == "_":
                regex_parts.append(".")
            else:
                regex_parts.append(re.escape(ch))
        regex = "^" + "".join(regex_parts) + "$"
        return bool(re.match(regex, str(row.get(col, "")), re.IGNORECASE))

    # IN (val, val, ...)
    m = re.match(r"(\w+)\s+IN\s*\(([^)]+)\)", condition, re.IGNORECASE)
    if m:
        col, vals_str = m.group(1), m.group(2)
        vals = [v.strip().strip("'\"") for v in vals_str.split(",")]
        cell = row.get(col)
        return any(_coerce(cell, v)[0] == _coerce(cell, v)[1] or str(cell) == v for v in vals)

    # standard OP — column name may be a plain identifier or an aggregate
    # expression like AVG(score) (after paren normalization above)
    m = re.match(r"([\w()]+)\s*(=|!=|<>|<=|>=|<|>)\s*'?([^']*)'?", condition)
    if m:
        col, op_str, raw = m.group(1), m.group(2), m.group(3)
        cell = row.get(col)
        a, b = _coerce(cell, raw)
        return _OPS[op_str](a, b)

    raise ValueError(f"Cannot parse condition: '{condition}'")


def _eval_where(row, where_clause):
    """
    Evaluate a WHERE clause against a row.

    Supports: NOT, AND / OR (with correct precedence: NOT > AND > OR),
    BETWEEN, IS NULL, LIKE, IN, comparison operators.

    Mod 4b: BETWEEN x AND y is rewritten to col >= x AND col <= y before
    splitting on AND/OR so the AND inside BETWEEN is not confused with a
    logical AND connector.

    Precedence: NOT binds tightest, then AND, then OR.
    "a OR b AND c" is evaluated as "a OR (b AND c)".
    """
    if not where_clause:
        return True

    # Convert BETWEEN...AND... to >= ... AND <= ... before any AND/OR split
    where_clause = re.sub(
        r"(\w+)\s+BETWEEN\s+(\S+)\s+AND\s+(\S+)",
        r"\1 >= \2 AND \1 <= \3",
        where_clause,
        flags=re.IGNORECASE,
    )

    # Split on OR first (lowest precedence), then AND within each OR-group
    or_groups = re.split(r"\s+OR\s+", where_clause, flags=re.IGNORECASE)
    for or_group in or_groups:
        and_parts = re.split(r"\s+AND\s+", or_group, flags=re.IGNORECASE)
        group_result = True
        for part in and_parts:
            part = part.strip()
            # Handle NOT prefix
            negated = False
            if re.match(r"^NOT\s+", part, re.IGNORECASE):
                negated = True
                part = re.sub(r"^NOT\s+", "", part, flags=re.IGNORECASE).strip()
            val = _eval_condition(row, part)
            if negated:
                val = not val
            group_result = group_result and val
        if group_result:
            return True   # short-circuit: one OR branch is True
    return False


# Hidden MVCC columns — these are stripped from user-facing results
_MVCC_COLS = {"_xmin", "_xmax"}


def _user_row(row: dict) -> dict:
    """Strip MVCC system columns from a row for user-facing output."""
    return {k: v for k, v in row.items() if k not in _MVCC_COLS}


class Table:

    def __init__(self, name, columns):
        self.name    = name
        self.columns : list[Column] = columns
        self.rows    : list[dict]   = []
        self.indexes : dict[str, Index] = {}

    # ------------------------------------------------------------------ #
    # MVCC visibility helpers
    # ------------------------------------------------------------------ #

    def visible_rows(self, txn_id: int = TXN_AUTO_COMMIT,
                     snapshot: Snapshot = None) -> list[dict]:
        """Return only rows visible to the given transaction."""
        if snapshot is None:
            # No snapshot: see all live rows (xmax == 0)
            return [r for r in self.rows
                    if r.get("_xmax", 0) == 0]
        return [r for r in self.rows
                if row_is_visible(r, txn_id, snapshot)]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _col(self, name) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"Column '{name}' not found in '{self.name}'")

    def _pk_col(self):
        for c in self.columns:
            if c.primary_key:
                return c
        return None

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def _validate_row(self, row: dict):
        for col in self.columns:
            val = row.get(col.name)

            if val is None and not col.nullable:
                raise ValueError(f"Column '{col.name}' cannot be NULL")

            if val is not None:
                if col.datatype == "INT" and not isinstance(val, int):
                    raise TypeError(f"Column '{col.name}' must be INT")
                if col.datatype == "FLOAT" and not isinstance(val, (int, float)):
                    raise TypeError(f"Column '{col.name}' must be FLOAT")
                if col.datatype == "BOOL" and not isinstance(val, bool):
                    raise TypeError(f"Column '{col.name}' must be BOOL")
                if col.datatype in ("TEXT", "DATE", "BLOB") and not isinstance(val, str):
                    raise TypeError(f"Column '{col.name}' must be {col.datatype}")

            if col.check and val is not None:
                expr = f"{val} {col.check}"
                if not eval(expr):  # noqa: S307  (trusted internal CHECK expr)
                    raise ValueError(
                        f"CHECK constraint failed on '{col.name}': {val} {col.check}"
                    )

    def _validate_unique(self, row: dict, txn_id=TXN_AUTO_COMMIT,
                         snapshot=None, exclude_row=None, engine=None):
        """Check UNIQUE/PK constraints — PostgreSQL-style behavior.

        Checks ALL alive rows (_xmax == 0), not just MVCC-visible ones.
        If a conflicting row was inserted by another ACTIVE (uncommitted)
        transaction, we wait for that transaction to finish:
          - If it COMMITs  → duplicate error (conflict is real)
          - If it ROLLBACKs → allow our insert (conflict gone)

        This prevents both false duplicates and unnecessary rejections.
        """
        import time

        for col in self.columns:
            if not (col.unique or col.primary_key):
                continue
            val = row.get(col.name)
            if val is None:
                continue

            for existing in self.rows:
                if existing.get("_xmax", 0) != 0:
                    continue  # dead row, skip
                if exclude_row is not None and existing is exclude_row:
                    continue
                if existing.get(col.name) != val:
                    continue

                # Conflict found! But who inserted it?
                conflict_xmin = existing.get("_xmin", TXN_AUTO_COMMIT)

                # If I inserted it myself, skip (e.g. UPDATE same row)
                if conflict_xmin == txn_id:
                    continue

                # If it's auto-committed or committed, it's a real duplicate
                if engine is None or conflict_xmin == TXN_AUTO_COMMIT:
                    constraint = "PRIMARY KEY" if col.primary_key else "UNIQUE"
                    raise ValueError(
                        f"{constraint} violation on '{col.name}': duplicate value '{val}'"
                    )

                mvcc = engine.txn_mgr.mvcc

                # If the conflicting txn already committed → real duplicate
                if mvcc.is_committed(conflict_xmin):
                    constraint = "PRIMARY KEY" if col.primary_key else "UNIQUE"
                    raise ValueError(
                        f"{constraint} violation on '{col.name}': duplicate value '{val}'"
                    )

                # If the conflicting txn already aborted → row is dead, skip
                if mvcc.is_aborted(conflict_xmin):
                    continue

                # Conflicting txn is ACTIVE — wait for it to finish
                # (PostgreSQL-style: block until commit or rollback)
                for _ in range(100):  # max ~5 seconds wait
                    time.sleep(0.05)
                    if mvcc.is_committed(conflict_xmin):
                        constraint = "PRIMARY KEY" if col.primary_key else "UNIQUE"
                        raise ValueError(
                            f"{constraint} violation on '{col.name}': duplicate value '{val}'"
                        )
                    if mvcc.is_aborted(conflict_xmin):
                        break  # conflict gone, allow our insert
                    # Still active? Check if row is still alive
                    if existing.get("_xmax", 0) != 0:
                        break  # row was deleted/rolled back
                else:
                    # Timeout — reject to avoid infinite wait
                    raise ValueError(
                        f"Timeout waiting for transaction {conflict_xmin} "
                        f"to resolve {col.name}='{val}' conflict"
                    )

    def _validate_foreign_keys(self, row: dict, engine,
                               txn_id=TXN_AUTO_COMMIT, snapshot=None):
        if engine is None:
            return
        for col in self.columns:
            if not col.foreign_key:
                continue
            val = row.get(col.name)
            if val is None:
                continue
            ref_table = engine.get_table(col.foreign_key["table"])
            ref_col   = col.foreign_key["column"]
            # Check against visible rows in referenced table
            visible = ref_table.visible_rows(txn_id, snapshot)
            exists = any(r.get(ref_col) == val for r in visible)
            if not exists:
                raise ValueError(
                    f"Foreign key violation: '{col.name}'={val} not found in "
                    f"{col.foreign_key['table']}.{ref_col}"
                )

    # ------------------------------------------------------------------ #
    # INSERT  (MVCC-aware)
    # ------------------------------------------------------------------ #

    def insert(self, values, engine=None, txn_id=TXN_AUTO_COMMIT,
               snapshot=None):
        if len(values) != len(self.columns):
            raise ValueError(
                f"Expected {len(self.columns)} values, got {len(values)}"
            )
        row = {}
        for col, val in zip(self.columns, values):
            if val is None and col.default is not None:
                val = col.cast(col.default)
            else:
                val = col.cast(val) if not isinstance(val, (int, float, bool, type(None))) else val
            row[col.name] = val

        self._validate_row(row)
        self._validate_unique(row, txn_id=txn_id, snapshot=snapshot, engine=engine)
        self._validate_foreign_keys(row, engine, txn_id=txn_id, snapshot=snapshot)

        # Stamp MVCC metadata
        stamp_insert(row, txn_id)

        self.rows.append(row)

        # Update indexes (use physical position)
        idx = len(self.rows) - 1
        for index in self.indexes.values():
            if index.is_composite:
                index.insert(idx, row)
            else:
                index.insert(idx, row.get(index.column_name))

        return row

    def insert_dict(self, data: dict, engine=None, txn_id=TXN_AUTO_COMMIT,
                    snapshot=None):
        """Insert by column-name dict (missing columns use their defaults)."""
        values = []
        for col in self.columns:
            if col.name in data:
                values.append(data[col.name])
            elif col.default is not None:
                values.append(col.default)
            else:
                values.append(None)
        return self.insert(values, engine, txn_id=txn_id, snapshot=snapshot)

    # ------------------------------------------------------------------ #
    # SELECT  (MVCC-aware)
    # ------------------------------------------------------------------ #

    def select(
        self,
        columns="*",
        where=None,
        order_by=None,
        order_dir="ASC",
        limit=None,
        offset=0,
        group_by=None,
        having=None,
        txn_id=TXN_AUTO_COMMIT,
        snapshot=None,
    ):
        # Get only visible rows
        visible = self.visible_rows(txn_id, snapshot)
        # Strip MVCC columns for WHERE evaluation and output
        rows = [_user_row(r) for r in visible if _eval_where(_user_row(r), where)]

        if group_by:
            rows = self._apply_group_by(rows, group_by, columns, having)
            return rows

        if order_by:
            reverse = order_dir.upper() == "DESC"
            rows = sorted(
                rows,
                key=lambda r: (r.get(order_by) is None, r.get(order_by)),
                reverse=reverse,
            )

        rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]

        if columns != "*":
            col_list = [c.strip() for c in columns.split(",")]
            rows = self._project(rows, col_list)

        return rows

    def _has_aggregate(self, col_list):
        return any(
            re.match(r"(COUNT|SUM|AVG|MIN|MAX)\(", spec, re.IGNORECASE)
            for spec in col_list
        )

    def _project(self, rows, col_list):
        if self._has_aggregate(col_list) and all(
            re.match(r"(COUNT|SUM|AVG|MIN|MAX)\(", s, re.IGNORECASE)
            for s in col_list
        ):
            out = {}
            for spec in col_list:
                agg_m = re.match(r"(COUNT|SUM|AVG|MIN|MAX)\((\*|\w+)\)", spec, re.IGNORECASE)
                fn, col = agg_m.group(1).upper(), agg_m.group(2)
                out[spec] = self._aggregate(fn, col, rows)
            return [out]

        result = []
        for row in rows:
            projected = {}
            for spec in col_list:
                agg_m = re.match(r"(COUNT|SUM|AVG|MIN|MAX)\((\*|\w+)\)", spec, re.IGNORECASE)
                if agg_m:
                    fn, col = agg_m.group(1).upper(), agg_m.group(2)
                    projected[spec] = self._aggregate(fn, col, rows)
                else:
                    projected[spec] = row.get(spec)
            result.append(projected)
        return result

    def _aggregate(self, fn, col, rows):
        if fn == "COUNT":
            return len(rows)
        vals = [r[col] for r in rows if r.get(col) is not None]
        if not vals:
            return None
        if fn == "SUM":
            return sum(vals)
        if fn == "AVG":
            return sum(vals) / len(vals)
        if fn == "MIN":
            return min(vals)
        if fn == "MAX":
            return max(vals)

    def _apply_group_by(self, rows, group_by, columns, having):
        groups: dict = {}
        for row in rows:
            key = tuple(row.get(c.strip()) for c in group_by.split(","))
            groups.setdefault(key, []).append(row)

        result   = []
        col_list = [c.strip() for c in columns.split(",")]
        for key, group_rows in groups.items():
            out_row = {}
            for spec in col_list:
                agg_m = re.match(r"(COUNT|SUM|AVG|MIN|MAX)\((\*|\w+)\)", spec, re.IGNORECASE)
                if agg_m:
                    fn, col = agg_m.group(1).upper(), agg_m.group(2)
                    out_row[spec] = self._aggregate(fn, col, group_rows)
                else:
                    out_row[spec] = group_rows[0].get(spec)
            if having and not _eval_where(out_row, having):
                continue
            result.append(out_row)
        return result

    def select_all(self, txn_id=TXN_AUTO_COMMIT, snapshot=None):
        """Return all visible rows (without MVCC columns)."""
        visible = self.visible_rows(txn_id, snapshot)
        return [_user_row(r) for r in visible]

    # ------------------------------------------------------------------ #
    # UPDATE  (MVCC-aware: delete old version + insert new version)
    # ------------------------------------------------------------------ #

    def update(self, assignments: dict, where=None, engine=None,
               txn_id=TXN_AUTO_COMMIT, snapshot=None):
        count = 0
        # Collect rows to update first (to avoid modifying while iterating)
        to_update = []
        for row in self.rows:
            if row.get("_xmax", 0) != 0:
                # Already deleted version — skip
                if snapshot and not row_is_visible(row, txn_id, snapshot):
                    continue
                elif not snapshot and row.get("_xmax", 0) != 0:
                    continue
            if snapshot and not row_is_visible(row, txn_id, snapshot):
                continue
            user_r = _user_row(row)
            if _eval_where(user_r, where):
                to_update.append(row)

        for old_row in to_update:
            # Build new row with updated values
            new_row = {}
            for col in self.columns:
                if col.name in assignments:
                    new_row[col.name] = col.cast(assignments[col.name])
                else:
                    new_row[col.name] = old_row.get(col.name)

            self._validate_row(new_row)
            self._validate_unique(new_row, txn_id=txn_id, snapshot=snapshot,
                                  exclude_row=old_row, engine=engine)
            self._validate_foreign_keys(new_row, engine, txn_id=txn_id, snapshot=snapshot)

            # MVCC: mark old version as deleted
            stamp_delete(old_row, txn_id)

            # MVCC: insert new version
            stamp_insert(new_row, txn_id)
            self.rows.append(new_row)

            # Update indexes: remove old, add new
            old_idx = self.rows.index(old_row)
            new_idx = len(self.rows) - 1
            for index in self.indexes.values():
                if index.is_composite:
                    index.delete(old_idx, old_row)
                    index.insert(new_idx, new_row)
                else:
                    index.delete(old_idx, old_row.get(index.column_name))
                    index.insert(new_idx, new_row.get(index.column_name))

            count += 1
        return count

    # ------------------------------------------------------------------ #
    # DELETE  (MVCC-aware: mark xmax instead of removing)
    # ------------------------------------------------------------------ #

    def delete(self, where=None, txn_id=TXN_AUTO_COMMIT, snapshot=None):
        count = 0
        for row in self.rows:
            if row.get("_xmax", 0) != 0:
                if snapshot and not row_is_visible(row, txn_id, snapshot):
                    continue
                elif not snapshot:
                    continue
            if snapshot and not row_is_visible(row, txn_id, snapshot):
                continue
            user_r = _user_row(row)
            if _eval_where(user_r, where):
                # MVCC: don't physically remove — just mark xmax
                stamp_delete(row, txn_id)
                # Remove from indexes
                idx = self.rows.index(row)
                for index in self.indexes.values():
                    if index.is_composite:
                        index.delete(idx, row)
                    else:
                        index.delete(idx, row.get(index.column_name))
                count += 1
        return count

    # ------------------------------------------------------------------ #
    # VACUUM — physically remove dead row versions
    # ------------------------------------------------------------------ #

    def vacuum(self, oldest_active: int, committed: set[int]) -> int:
        """
        Remove dead row versions that no active transaction can see.
        Returns the number of rows removed.
        """
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if not is_dead(r, oldest_active, committed)]
        removed = before - len(self.rows)

        # Rebuild all indexes from scratch after physical removal
        if removed > 0:
            for index in self.indexes.values():
                index.build(self.rows)

        return removed

    # ------------------------------------------------------------------ #
    # ROLLBACK support — undo changes made by a specific transaction
    # ------------------------------------------------------------------ #

    def rollback_txn(self, txn_id: int):
        """
        Undo all changes made by txn_id:
        - Remove rows where _xmin == txn_id (rows created by this txn)
        - Restore rows where _xmax == txn_id (rows deleted by this txn)
        """
        # Restore deleted rows
        for row in self.rows:
            if row.get("_xmax") == txn_id:
                row["_xmax"] = 0

        # Remove inserted rows
        self.rows = [r for r in self.rows if r.get("_xmin") != txn_id]

        # Rebuild indexes
        for index in self.indexes.values():
            index.build(self.rows)

    # ------------------------------------------------------------------ #
    # ALTER TABLE
    # ------------------------------------------------------------------ #

    def add_column(self, column: Column):
        for c in self.columns:
            if c.name == column.name:
                raise ValueError(f"Column '{column.name}' already exists")
        self.columns.append(column)
        default_val = column.cast(column.default) if column.default is not None else None
        for row in self.rows:
            row[column.name] = default_val

    def drop_column(self, col_name: str):
        col = self._col(col_name)
        if col.primary_key:
            raise ValueError("Cannot drop PRIMARY KEY column")
        self.columns = [c for c in self.columns if c.name != col_name]
        for row in self.rows:
            row.pop(col_name, None)
        to_drop = [k for k, idx in self.indexes.items() if idx.column_name == col_name]
        for k in to_drop:
            del self.indexes[k]

    def rename_column(self, old_name: str, new_name: str):
        col = self._col(old_name)
        if any(c.name == new_name for c in self.columns):
            raise ValueError(f"Column '{new_name}' already exists")
        col.name = new_name
        for row in self.rows:
            row[new_name] = row.pop(old_name, None)
        for index in self.indexes.values():
            if index.column_name == old_name:
                index.column_name = new_name

    def truncate(self):
        count = len([r for r in self.rows if r.get("_xmax", 0) == 0])
        self.rows = []
        for index in self.indexes.values():
            index._data = {}
        return count

    # ------------------------------------------------------------------ #
    # Indexes
    # ------------------------------------------------------------------ #

    def create_index(self, index_name, col_name, unique=False):
        """col_name can be a string or a list of strings for composite index."""
        if index_name in self.indexes:
            raise ValueError(f"Index '{index_name}' already exists")
        # Validate all columns exist
        if isinstance(col_name, list):
            for c in col_name:
                self._col(c)
        else:
            self._col(col_name)
        idx = Index(index_name, self.name, col_name, unique)
        idx.build(self.rows)
        self.indexes[index_name] = idx
        return idx

    def drop_index(self, index_name):
        if index_name not in self.indexes:
            raise KeyError(f"Index '{index_name}' not found")
        del self.indexes[index_name]

    # ------------------------------------------------------------------ #
    # Schema info
    # ------------------------------------------------------------------ #

    def describe(self):
        return [
            {
                "Column":   c.name,
                "Type":     c.datatype,
                "PK":       c.primary_key,
                "Nullable": c.nullable,
                "Unique":   c.unique,
                "Default":  c.default,
                "Check":    c.check,
                "FK": (
                    f"{c.foreign_key['table']}.{c.foreign_key['column']}"
                    if c.foreign_key else None
                ),
            }
            for c in self.columns
        ]

    # ------------------------------------------------------------------ #
    # JOIN helpers  —  Mod 6: added CROSS JOIN
    # ------------------------------------------------------------------ #

    def join(self, other: "Table", on_left: str, on_right: str,
             join_type="INNER", txn_id=TXN_AUTO_COMMIT, snapshot=None):
        """
        Supported join types:
          INNER  — only rows with matching keys on both sides
          LEFT   — all left rows; right columns NULL when no match
          RIGHT  — all right rows; left columns NULL when no match
          FULL   — all rows from both sides
          CROSS  — cartesian product (on_left / on_right are ignored)
        """
        # Use visible rows only
        left_rows = [_user_row(r) for r in self.visible_rows(txn_id, snapshot)]
        right_rows = [_user_row(r) for r in other.visible_rows(txn_id, snapshot)]

        # Mod 6: CROSS JOIN — cartesian product, no ON condition needed
        if join_type == "CROSS":
            result = []
            for left_row in left_rows:
                for right_row in right_rows:
                    merged = {f"{self.name}.{k}": v for k, v in left_row.items()}
                    merged.update(
                        {f"{other.name}.{k}": v for k, v in right_row.items()}
                    )
                    result.append(merged)
            return result

        def _bare(col, table_name):
            prefix = table_name + "."
            return col[len(prefix):] if col.lower().startswith(prefix.lower()) else col

        left_col  = _bare(on_left,  self.name)
        right_col = _bare(on_right, other.name)

        result = []
        for left_row in left_rows:
            matched = False
            for right_row in right_rows:
                if left_row.get(left_col) == right_row.get(right_col):
                    merged = {f"{self.name}.{k}": v for k, v in left_row.items()}
                    merged.update(
                        {f"{other.name}.{k}": v for k, v in right_row.items()}
                    )
                    result.append(merged)
                    matched = True
            if not matched and join_type in ("LEFT", "FULL"):
                merged = {f"{self.name}.{k}": v for k, v in left_row.items()}
                merged.update({f"{other.name}.{c.name}": None for c in other.columns})
                result.append(merged)

        if join_type in ("RIGHT", "FULL"):
            left_keys = {lr.get(left_col) for lr in left_rows}
            for right_row in right_rows:
                if right_row.get(right_col) not in left_keys:
                    merged = {f"{self.name}.{c.name}": None for c in self.columns}
                    merged.update(
                        {f"{other.name}.{k}": v for k, v in right_row.items()}
                    )
                    result.append(merged)
        return result
