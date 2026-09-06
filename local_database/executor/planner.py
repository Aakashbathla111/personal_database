"""
Query Planner  —  executor/planner.py

Decides HOW to execute a query before the Executor runs it.

Decision tree for SELECT
────────────────────────
  Does WHERE contain a simple equality on an indexed column?
    YES  →  IndexScanPlan      O(log n)
  Does WHERE contain a single range condition on an indexed column?
    YES  →  RangeScanPlan      O(log n + k)   ← Mod 4c
  Anything else (JOIN, compound, OR, …)
    →  FullScanPlan            O(n)

Plans
─────
  FullScanPlan   — sequential scan of every row
  IndexScanPlan  — B+ tree exact-match lookup
  RangeScanPlan  — B+ tree range lookup (>=, <=, >, <, BETWEEN)

Mod 7 — Structured EXPLAIN
────────────────────────────
Every plan now carries a `explain()` method that returns a dict
describing the plan in human-readable form with estimated row counts.
Call it when the user types EXPLAIN ON.
"""

import re
from dataclasses import dataclass, field


@dataclass
class FullScanPlan:
    table:     str
    columns:   str  = "*"
    where:     str  = None
    order_by:  str  = None
    order_dir: str  = "ASC"
    limit:     int  = None
    offset:    int  = 0
    group_by:  str  = None
    having:    str  = None
    join:      dict = None
    note:      str  = "full scan"
    _est_rows: int  = field(default=0, repr=False)

    def explain(self) -> dict:
        plan = {
            "type":          "FullScan",
            "table":         self.table,
            "filter":        self.where or "(none)",
            "est_rows":      self._est_rows,
            "columns":       self.columns,
        }
        if self.join:
            plan["join"] = {
                "type":        self.join.get("type"),
                "right_table": self.join.get("right_table"),
                "on":          f"{self.join.get('on_left')} = {self.join.get('on_right')}",
            }
        if self.order_by:
            plan["order"] = f"{self.order_by} {self.order_dir}"
        if self.limit is not None:
            plan["limit"] = self.limit
        if self.group_by:
            plan["group_by"] = self.group_by
        return plan


@dataclass
class IndexScanPlan:
    table:      str
    index_name: str
    column:     str
    value:      object
    columns:    str  = "*"
    where:      str  = None
    order_by:   str  = None
    order_dir:  str  = "ASC"
    limit:      int  = None
    offset:     int  = 0
    note:       str  = "index scan"
    _est_rows:  int  = field(default=1, repr=False)

    def explain(self) -> dict:
        plan = {
            "type":      "IndexScan",
            "table":     self.table,
            "index":     self.index_name,
            "lookup":    f"{self.column} = {self.value!r}",
            "est_rows":  self._est_rows,
            "columns":   self.columns,
        }
        if self.order_by:
            plan["order"] = f"{self.order_by} {self.order_dir}"
        if self.limit is not None:
            plan["limit"] = self.limit
        return plan


# Mod 4c: range scan plan
@dataclass
class RangeScanPlan:
    """
    Uses the B+ tree's range_search() to find all rows where
    lo OP col OP hi  without scanning the whole table.

    Fields
    ------
    lo / hi          : bound values (None = open-ended)
    lo_inclusive     : True → >= , False → >
    hi_inclusive     : True → <= , False → <
    """
    table:        str
    index_name:   str
    column:       str
    lo:           object = None
    hi:           object = None
    lo_inclusive: bool   = True
    hi_inclusive: bool   = True
    columns:      str    = "*"
    where:        str    = None
    order_by:     str    = None
    order_dir:    str    = "ASC"
    limit:        int    = None
    offset:       int    = 0
    note:         str    = "range scan"
    _est_rows:    int    = field(default=0, repr=False)

    def explain(self) -> dict:
        lo_sym = ">=" if self.lo_inclusive else ">"
        hi_sym = "<=" if self.hi_inclusive else "<"
        bounds = []
        if self.lo is not None:
            bounds.append(f"{self.column} {lo_sym} {self.lo!r}")
        if self.hi is not None:
            bounds.append(f"{self.column} {hi_sym} {self.hi!r}")
        return {
            "type":      "RangeScan",
            "table":     self.table,
            "index":     self.index_name,
            "range":     " AND ".join(bounds) or "(unbounded)",
            "est_rows":  self._est_rows,
            "columns":   self.columns,
        }


class QueryPlanner:

    def plan(self, node, engine):
        from parser.ast import SelectNode
        if not isinstance(node, SelectNode):
            return None

        # Row count estimate for the table (used in EXPLAIN output)
        est_rows = 0
        if node.table in engine.tables:
            est_rows = len(engine.tables[node.table].rows)
        elif node.table in engine.views:
            base = engine.views[node.table].get("base_table", "")
            est_rows = len(engine.tables.get(base, type("", (), {"rows": []})()).rows)

        # JOIN → always full scan (both sides)
        if node.join:
            return FullScanPlan(
                table=node.table, columns=node.columns,
                where=node.where, order_by=node.order_by,
                order_dir=node.order_dir, limit=node.limit,
                offset=node.offset, join=node.join,
                note="full scan (join)",
                _est_rows=est_rows,
            )

        # Try index plans only when a WHERE clause exists on a real table
        if node.where and node.table in engine.tables:
            # 1. Exact match → IndexScanPlan
            plan = self._try_index_exact(node, engine, est_rows)
            if plan:
                return plan
            # 2. Range condition → RangeScanPlan  (Mod 4c)
            plan = self._try_index_range(node, engine, est_rows)
            if plan:
                return plan

        return FullScanPlan(
            table=node.table, columns=node.columns,
            where=node.where, order_by=node.order_by,
            order_dir=node.order_dir, limit=node.limit,
            offset=node.offset, group_by=node.group_by,
            having=node.having, _est_rows=est_rows,
        )

    # ------------------------------------------------------------------ #
    # Index exact match
    # ------------------------------------------------------------------ #

    def _try_index_exact(self, node, engine, est_rows) -> IndexScanPlan | None:
        m = re.fullmatch(r"(\w+)\s*=\s*'?([^']+)'?", node.where.strip())
        if not m:
            return None

        col_name, raw_val = m.group(1), m.group(2).strip("'\"")
        table = engine.tables[node.table]

        for idx_name, idx in table.indexes.items():
            if idx.column_name == col_name:
                try:
                    typed_val = table._col(col_name).cast(raw_val)
                except Exception:
                    typed_val = raw_val

                return IndexScanPlan(
                    table=node.table,
                    index_name=idx_name,
                    column=col_name,
                    value=typed_val,
                    columns=node.columns,
                    where=node.where,
                    order_by=node.order_by,
                    order_dir=node.order_dir,
                    limit=node.limit,
                    offset=node.offset,
                    note=f"index scan on '{idx_name}'",
                    _est_rows=1,
                )
        return None

    # ------------------------------------------------------------------ #
    # Mod 4c — Range scan
    # ------------------------------------------------------------------ #

    def _try_index_range(self, node, engine, est_rows) -> RangeScanPlan | None:
        """
        Detect single-sided range conditions and compound (BETWEEN-style)
        conditions that map to a B+ tree range search.

        Patterns recognised (all anchored to the full WHERE string):
          col >= val           →  lo=val, hi=None, lo_inclusive=True
          col >  val           →  lo=val, hi=None, lo_inclusive=False
          col <= val           →  lo=None, hi=val, hi_inclusive=True
          col <  val           →  lo=None, hi=val, hi_inclusive=False
          col >= lo AND col <= hi  →  compound range (BETWEEN rewritten form)
          col >  lo AND col <  hi  →  compound exclusive range
        """
        where = node.where.strip()
        table = engine.tables[node.table]

        # Compound range: col OP lo AND col OP hi
        compound = re.fullmatch(
            r"(\w+)\s*(>=|>)\s*'?([^'\s]+)'?\s+AND\s+\1\s*(<=|<)\s*'?([^'\s]+)'?",
            where, re.IGNORECASE,
        )
        if compound:
            col_name = compound.group(1)
            lo_op    = compound.group(2)
            lo_val   = compound.group(3).strip("'\"")
            hi_op    = compound.group(4)
            hi_val   = compound.group(5).strip("'\"")

            for idx_name, idx in table.indexes.items():
                if idx.column_name == col_name:
                    try:
                        col       = table._col(col_name)
                        lo_typed  = col.cast(lo_val)
                        hi_typed  = col.cast(hi_val)
                    except Exception:
                        lo_typed, hi_typed = lo_val, hi_val

                    return RangeScanPlan(
                        table=node.table,
                        index_name=idx_name,
                        column=col_name,
                        lo=lo_typed,
                        hi=hi_typed,
                        lo_inclusive=(lo_op == ">="),
                        hi_inclusive=(hi_op == "<="),
                        columns=node.columns,
                        where=node.where,
                        order_by=node.order_by,
                        order_dir=node.order_dir,
                        limit=node.limit,
                        offset=node.offset,
                        note=f"range scan on '{idx_name}'",
                        _est_rows=max(1, est_rows // 4),
                    )

        # Single-sided range: col OP val
        single = re.fullmatch(
            r"(\w+)\s*(>=|<=|>|<)\s*'?([^']+)'?", where
        )
        if single:
            col_name = single.group(1)
            op       = single.group(2)
            raw_val  = single.group(3).strip("'\"")

            for idx_name, idx in table.indexes.items():
                if idx.column_name == col_name:
                    try:
                        typed_val = table._col(col_name).cast(raw_val)
                    except Exception:
                        typed_val = raw_val

                    lo = typed_val if op in (">", ">=") else None
                    hi = typed_val if op in ("<", "<=") else None

                    return RangeScanPlan(
                        table=node.table,
                        index_name=idx_name,
                        column=col_name,
                        lo=lo,
                        hi=hi,
                        lo_inclusive=(op == ">="),
                        hi_inclusive=(op == "<="),
                        columns=node.columns,
                        where=node.where,
                        order_by=node.order_by,
                        order_dir=node.order_dir,
                        limit=node.limit,
                        offset=node.offset,
                        note=f"range scan on '{idx_name}'",
                        _est_rows=max(1, est_rows // 2),
                    )

        return None
