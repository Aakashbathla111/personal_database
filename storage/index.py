"""
Index  —  storage/index.py

Wraps the B+ Tree behind a clean API called by the storage engine.

Supports both single-column and composite (multi-column) indexes.

Single:    CREATE INDEX idx ON employees(department)
Composite: CREATE INDEX idx ON employees(department, age)

Composite index key = tuple of column values, sorted left-to-right.
Follows the left-most prefix rule:
  Index on (A, B, C) can speed up:
    WHERE A = x                    ✅
    WHERE A = x AND B = y          ✅
    WHERE A = x AND B = y AND C = z ✅
    WHERE B = y                    ❌ (A skipped)
"""

from index.bplustree import BPlusTree


class Index:

    def __init__(self, name, table_name, column_name, unique=False):
        self.name        = name
        self.table_name  = table_name
        # column_name can be str ("dept") or list ["dept", "age"] for composite
        self.column_name = column_name
        self.unique      = unique
        self._tree       = BPlusTree()

    @property
    def is_composite(self) -> bool:
        return isinstance(self.column_name, list)

    @property
    def columns(self) -> list[str]:
        """Always return column names as a list."""
        if isinstance(self.column_name, list):
            return self.column_name
        return [self.column_name]

    # ------------------------------------------------------------------ #
    # Key building — single or composite
    # ------------------------------------------------------------------ #

    def _row_key(self, row: dict):
        """Extract the B+ tree key from a row dict."""
        if self.is_composite:
            return tuple(self._to_key(row.get(c)) for c in self.column_name)
        return self._to_key(row.get(self.column_name))

    def _val_key(self, value):
        """Convert a lookup value to a B+ tree key.
        For composite: value should be a tuple/list of values.
        For single: value is a scalar."""
        if self.is_composite:
            if isinstance(value, (tuple, list)):
                return tuple(self._to_key(v) for v in value)
            return (self._to_key(value),)
        return self._to_key(value)

    # ------------------------------------------------------------------ #
    # Build (called on startup to reconstruct from saved rows)
    # ------------------------------------------------------------------ #

    def build(self, rows):
        """Scan every row and insert into the B+ tree."""
        self._tree = BPlusTree()
        for i, row in enumerate(rows):
            key = self._row_key(row)
            if self.unique and self._tree.search(key):
                raise ValueError(
                    f"UNIQUE index '{self.name}' violated: "
                    f"duplicate value '{key}'"
                )
            self._tree.insert(key, i)

    # ------------------------------------------------------------------ #
    # Lookup (exact match)
    # ------------------------------------------------------------------ #

    def lookup(self, value) -> list[int]:
        """Return list of row indices whose column value == value  O(log n).
        For composite index, value should be a tuple of all column values."""
        key = self._val_key(value)
        return self._tree.search(key)

    def prefix_lookup(self, values: list) -> list[int]:
        """Composite index only: lookup by left-most prefix columns.
        E.g., index on (dept, age) → prefix_lookup(['Engineering'])
        returns all rows where dept='Engineering' (any age).
        Uses range scan: ('engineering',) <= key < ('engineering' + 1,)
        """
        if not self.is_composite:
            return self.lookup(values[0])

        prefix = tuple(self._to_key(v) for v in values)
        # Collect all entries that start with this prefix
        result = []
        leaf = self._tree._leftmost_leaf()
        while leaf:
            for i, k in enumerate(leaf.keys):
                if isinstance(k, tuple) and k[:len(prefix)] == prefix:
                    result.extend(leaf.values[i])
                elif isinstance(k, tuple) and k[:len(prefix)] > prefix:
                    return result  # past our prefix, done
            leaf = leaf.next
        return result

    # ------------------------------------------------------------------ #
    # Range lookup
    # ------------------------------------------------------------------ #

    def range_lookup(self, lo=None, hi=None,
                     lo_inclusive=True, hi_inclusive=True) -> list[int]:
        lo_key = self._val_key(lo) if lo is not None else None
        hi_key = self._val_key(hi) if hi is not None else None
        return self._tree.range_search(
            lo_key, hi_key,
            lo_inclusive=lo_inclusive,
            hi_inclusive=hi_inclusive,
        )

    # ------------------------------------------------------------------ #
    # Insert (called after every row insert)
    # ------------------------------------------------------------------ #

    def insert(self, row_index: int, value):
        """For composite: value should be a dict (the row) or use _row_key externally."""
        if isinstance(value, dict):
            key = self._row_key(value)
        else:
            key = self._val_key(value)
        if self.unique and self._tree.search(key):
            raise ValueError(
                f"UNIQUE index '{self.name}' violated: "
                f"duplicate value '{value}'"
            )
        self._tree.insert(key, row_index)

    # ------------------------------------------------------------------ #
    # Delete (called before a row is removed)
    # ------------------------------------------------------------------ #

    def delete(self, row_index: int, value):
        if isinstance(value, dict):
            key = self._row_key(value)
        else:
            key = self._val_key(value)
        self._tree.delete(key, row_index)

    # ------------------------------------------------------------------ #
    # Shift row indices after a physical delete
    # ------------------------------------------------------------------ #

    def rebuild_after_delete(self, deleted_index: int):
        """
        After a row is physically popped from table.rows, all stored
        indices > deleted_index must be decremented by 1.
        """
        self._tree.rebuild_after_delete(deleted_index)

    # ------------------------------------------------------------------ #
    # Snapshot / restore  (used by TransactionManager for ROLLBACK)
    # ------------------------------------------------------------------ #

    @property
    def _data(self):
        """Return the B+ tree contents as a plain dict so deepcopy works."""
        return {str(k): list(v) for k, v in self._tree.all_entries()}

    @_data.setter
    def _data(self, snapshot: dict):
        """Restore the B+ tree from a snapshot dict (used on ROLLBACK)."""
        self._tree = BPlusTree()
        for key_str, indices in snapshot.items():
            key = self._parse_key(key_str)
            for idx in indices:
                self._tree.insert(key, idx)

    # ------------------------------------------------------------------ #
    # Persistence helpers
    # ------------------------------------------------------------------ #

    def to_dict(self):
        """Only metadata is persisted; tree is rebuilt from rows on load."""
        return {
            "name":        self.name,
            "table_name":  self.table_name,
            "column_name": self.column_name,  # str or list
            "unique":      self.unique,
        }

    @staticmethod
    def from_dict(data):
        return Index(
            name=data["name"],
            table_name=data["table_name"],
            column_name=data["column_name"],  # str or list — both work
            unique=data.get("unique", False),
        )

    # ------------------------------------------------------------------ #
    # Key helpers  (B+ tree keys must be comparable)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_key(value):
        """
        Convert a column value to a comparable B+ tree key.
        Numeric values stay numeric so range comparisons are correct.
        Strings are lowercased for case-insensitive ordering.
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        try:
            return int(value)
        except (ValueError, TypeError):
            pass
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
        return str(value).lower()

    @staticmethod
    def _parse_key(key_str: str):
        """Reverse _to_key for snapshot restore."""
        try:
            return int(key_str)
        except (ValueError, TypeError):
            pass
        try:
            return float(key_str)
        except (ValueError, TypeError):
            pass
        return key_str
