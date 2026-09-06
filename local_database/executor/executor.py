"""
Executor  —  executor/executor.py

The only layer that talks directly to the StorageEngine.

Full pipeline
─────────────
  SQL text
    └─► Lexer.tokenize()      → tokens
          └─► Parser.parse()  → AST node
                └─► Planner.plan()  → execution plan  (for SELECT)
                      └─► Executor.execute()  → result / side-effect

Multi-session support is handled by thread-local storage in
TransactionManager — no session_id needed here.
"""

import re

from parser.ast import (
    SelectNode, InsertNode, UpdateNode, DeleteNode,
    CreateTableNode, DropTableNode, TruncateNode, RenameTableNode,
    AlterAddColumnNode, AlterDropColumnNode, AlterRenameColumnNode,
    CreateIndexNode, DropIndexNode, ShowIndexesNode,
    CreateViewNode, DropViewNode, ShowViewsNode,
    BeginNode, CommitNode, RollbackNode,
    SavepointNode, RollbackToSavepointNode, ReleaseSavepointNode,
    ShowTablesNode, DescribeNode, RecoverNode, VacuumNode, ExitNode,
)
from executor.planner import QueryPlanner, FullScanPlan, IndexScanPlan, RangeScanPlan


class Executor:

    def __init__(self, engine):
        self.engine  = engine
        self.planner = QueryPlanner()

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #

    def execute(self, node):
        """
        Execute *node* against the engine.
        Returns a result dict: {rows, count, message, plan, explain}.
        """
        # ── SELECT ────────────────────────────────────────────────────
        if isinstance(node, SelectNode):
            node.where = self._resolve_subqueries(node.where)
            return self._exec_select(node)

        # ── INSERT ────────────────────────────────────────────────────
        if isinstance(node, InsertNode):
            if node.multi_rows:
                count = 0
                for vals in node.multi_rows:
                    if node.columns:
                        data = dict(zip(node.columns, vals))
                        self.engine.insert_named(node.table, data)
                    else:
                        self.engine.insert(node.table, vals)
                    count += 1
                return {"count": count, "message": f"{count} row(s) inserted."}
            if node.columns:
                data = dict(zip(node.columns, node.values))
                self.engine.insert_named(node.table, data)
            else:
                self.engine.insert(node.table, node.values)
            return {"count": 1, "message": "1 row inserted."}

        # ── UPDATE ────────────────────────────────────────────────────
        if isinstance(node, UpdateNode):
            node.where = self._resolve_subqueries(node.where)
            n = self.engine.update(node.table, node.assignments, where=node.where)
            return {"count": n, "message": f"{n} row(s) updated."}

        # ── DELETE ────────────────────────────────────────────────────
        if isinstance(node, DeleteNode):
            node.where = self._resolve_subqueries(node.where)
            n = self.engine.delete(node.table, where=node.where)
            return {"count": n, "message": f"{n} row(s) deleted."}

        # ── DDL ───────────────────────────────────────────────────────
        if isinstance(node, CreateTableNode):
            self.engine.create_table(node.table, node.columns)
            return {"message": f"Table '{node.table}' created."}

        if isinstance(node, DropTableNode):
            self.engine.drop_table(node.table, if_exists=node.if_exists)
            return {"message": f"Table '{node.table}' dropped."}

        if isinstance(node, TruncateNode):
            n = self.engine.truncate(node.table)
            return {"message": f"Truncated {n} row(s) from '{node.table}'."}

        if isinstance(node, RenameTableNode):
            self.engine.rename_table(node.old, node.new)
            return {"message": f"Table renamed '{node.old}' → '{node.new}'."}

        if isinstance(node, AlterAddColumnNode):
            self.engine.alter_add_column(node.table, node.column)
            return {"message": f"Column '{node.column.name}' added to '{node.table}'."}

        if isinstance(node, AlterDropColumnNode):
            self.engine.alter_drop_column(node.table, node.column)
            return {"message": f"Column '{node.column}' dropped from '{node.table}'."}

        if isinstance(node, AlterRenameColumnNode):
            self.engine.alter_rename_column(node.table, node.old, node.new)
            return {"message": f"Column '{node.old}' → '{node.new}' in '{node.table}'."}

        # ── INDEXES ───────────────────────────────────────────────────
        if isinstance(node, CreateIndexNode):
            self.engine.create_index(node.index, node.table, node.column, node.unique)
            col_display = ", ".join(node.column) if isinstance(node.column, list) else node.column
            return {"message": f"Index '{node.index}' created on "
                               f"'{node.table}({col_display})'."}

        if isinstance(node, DropIndexNode):
            self.engine.drop_index(node.index, node.table)
            return {"message": f"Index '{node.index}' dropped."}

        if isinstance(node, ShowIndexesNode):
            return {"rows": self.engine.show_indexes(node.table)}

        # ── VIEWS ─────────────────────────────────────────────────────
        if isinstance(node, CreateViewNode):
            self.engine.create_view(node.view, node.base_table,
                                    node.columns, node.where)
            return {"message": f"View '{node.view}' created."}

        if isinstance(node, DropViewNode):
            self.engine.drop_view(node.view, if_exists=node.if_exists)
            return {"message": f"View '{node.view}' dropped."}

        if isinstance(node, ShowViewsNode):
            return {"list": self.engine.show_views()}

        # ── TRANSACTIONS ──────────────────────────────────────────────
        if isinstance(node, BeginNode):
            self.engine.begin()
            return {"message": "Transaction started."}

        if isinstance(node, CommitNode):
            self.engine.commit()
            return {"message": "Transaction committed."}

        if isinstance(node, RollbackNode):
            self.engine.rollback()
            return {"message": "Transaction rolled back."}

        if isinstance(node, SavepointNode):
            self.engine.txn_mgr.savepoint(node.name)
            return {"message": f"Savepoint '{node.name}' created."}

        if isinstance(node, RollbackToSavepointNode):
            self.engine.txn_mgr.rollback_to(node.name)
            return {"message": f"Rolled back to savepoint '{node.name}'."}

        if isinstance(node, ReleaseSavepointNode):
            self.engine.txn_mgr.release_savepoint(node.name)
            return {"message": f"Savepoint '{node.name}' released."}

        # ── INFO ──────────────────────────────────────────────────────
        if isinstance(node, ShowTablesNode):
            return {"list": self.engine.show_tables()}

        if isinstance(node, DescribeNode):
            return {"rows": self.engine.describe(node.table)}

        if isinstance(node, RecoverNode):
            self.engine.recover()
            return {"message": ""}

        if isinstance(node, VacuumNode):
            result = self.engine.vacuum()
            removed = result["removed"]
            return {"message": f"VACUUM complete: {removed} dead row version(s) removed."}

        if isinstance(node, ExitNode):
            return {"exit": True}

        raise RuntimeError(f"Executor: unhandled AST node {type(node).__name__}")

    # ------------------------------------------------------------------ #
    # Subquery resolution
    # ------------------------------------------------------------------ #

    def _resolve_subqueries(self, where: str) -> str:
        if not where or "SELECT" not in where.upper():
            return where

        m = re.search(r"(\w+)\s+IN\s*\(", where, re.IGNORECASE)
        if not m:
            return where

        paren_open = m.end() - 1
        inner_start = paren_open + 1

        rest = where[inner_start:].lstrip()
        if not rest.upper().startswith("SELECT"):
            return where

        col    = m.group(1)
        prefix = where[:m.start()]

        depth = 1
        i = inner_start
        while i < len(where) and depth > 0:
            if where[i] == "(":
                depth += 1
            elif where[i] == ")":
                depth -= 1
            i += 1
        inner_sql = where[inner_start : i - 1].strip()
        suffix    = where[i:]

        from parser.parser import Parser
        sub_node = Parser().parse(inner_sql)
        sub_result = self._exec_select(sub_node)
        sub_rows   = sub_result.get("rows", [])

        if not sub_rows:
            in_list = "NULL"
        else:
            first_col = list(sub_rows[0].keys())[0]
            vals = []
            for row in sub_rows:
                v = row.get(first_col)
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, str):
                    vals.append(f"'{v}'")
                else:
                    vals.append(str(v))
            in_list = ", ".join(vals)

        resolved = f"{prefix}{col} IN ({in_list}){suffix}"
        return self._resolve_subqueries(resolved)

    # ------------------------------------------------------------------ #
    # SELECT execution (uses planner)
    # ------------------------------------------------------------------ #

    def _exec_select(self, node: SelectNode):
        plan = self.planner.plan(node, self.engine)

        explain_dict = plan.explain() if hasattr(plan, "explain") else {}

        if isinstance(plan, RangeScanPlan):
            rows = self._range_scan(plan)
            rows = self._apply_post_select(rows, node)
            return {"rows": rows, "plan": plan.note, "explain": explain_dict}

        if isinstance(plan, IndexScanPlan):
            rows = self._index_scan(plan)
            rows = self._apply_post_select(rows, node)
            return {"rows": rows, "plan": plan.note, "explain": explain_dict}

        if plan.join:
            j    = plan.join
            rows = self.engine.join(
                left_table=plan.table,
                right_table=j["right_table"],
                on_left=j["on_left"],
                on_right=j["on_right"],
                join_type=j["type"],
                where=plan.where,
                columns=plan.columns,
                order_by=plan.order_by,
                order_dir=plan.order_dir,
                limit=plan.limit,
            )
        else:
            rows = self.engine.select(
                plan.table,
                columns=plan.columns,
                where=plan.where,
                order_by=plan.order_by,
                order_dir=plan.order_dir,
                limit=plan.limit,
                offset=plan.offset,
                group_by=plan.group_by,
                having=plan.having,
            )

        rows = self._apply_post_select(rows, node)
        return {"rows": rows, "plan": plan.note, "explain": explain_dict}

    # ------------------------------------------------------------------ #
    # Post-SELECT: DISTINCT and column aliases
    # ------------------------------------------------------------------ #

    def _apply_post_select(self, rows: list[dict], node: SelectNode) -> list[dict]:
        if node.distinct and rows:
            seen = []
            unique = []
            for row in rows:
                key = tuple(sorted(row.items()))
                if key not in seen:
                    seen.append(key)
                    unique.append(row)
            rows = unique

        if node.aliases and rows:
            reverse = {v: k for k, v in node.aliases.items()}
            rows = [
                {reverse.get(k, k): v for k, v in row.items()}
                for row in rows
            ]

        return rows

    # ------------------------------------------------------------------ #
    # Index exact-match scan
    # ------------------------------------------------------------------ #

    def _index_scan(self, plan: IndexScanPlan) -> list[dict]:
        from storage.table import _eval_where, _user_row
        from storage.mvcc import row_is_visible

        table       = self.engine.get_table(plan.table)
        idx         = table.indexes[plan.index_name]
        row_indices = idx.lookup(plan.value)

        txn_id = self.engine.current_txn_id
        snap   = self.engine.current_snapshot

        raw_rows = [table.rows[i] for i in row_indices if i < len(table.rows)]
        if snap:
            raw_rows = [r for r in raw_rows if row_is_visible(r, txn_id, snap)]
        else:
            raw_rows = [r for r in raw_rows if r.get("_xmax", 0) == 0]
        rows = [_user_row(r) for r in raw_rows]

        if plan.where:
            rows = [r for r in rows if _eval_where(r, plan.where)]

        if plan.order_by:
            reverse = plan.order_dir.upper() == "DESC"
            rows = sorted(
                rows,
                key=lambda r: (r.get(plan.order_by) is None, r.get(plan.order_by)),
                reverse=reverse,
            )

        rows = rows[plan.offset:]
        if plan.limit is not None:
            rows = rows[:plan.limit]

        if plan.columns != "*":
            col_list = [c.strip() for c in plan.columns.split(",")]
            rows = [{c: r.get(c) for c in col_list} for r in rows]

        return rows

    # ------------------------------------------------------------------ #
    # Range scan
    # ------------------------------------------------------------------ #

    def _range_scan(self, plan: RangeScanPlan) -> list[dict]:
        from storage.table import _eval_where, _user_row
        from storage.mvcc import row_is_visible

        table       = self.engine.get_table(plan.table)
        idx         = table.indexes[plan.index_name]
        row_indices = idx.range_lookup(
            lo=plan.lo,
            hi=plan.hi,
            lo_inclusive=plan.lo_inclusive,
            hi_inclusive=plan.hi_inclusive,
        )

        txn_id = self.engine.current_txn_id
        snap   = self.engine.current_snapshot

        raw_rows = [table.rows[i] for i in row_indices if i < len(table.rows)]
        if snap:
            raw_rows = [r for r in raw_rows if row_is_visible(r, txn_id, snap)]
        else:
            raw_rows = [r for r in raw_rows if r.get("_xmax", 0) == 0]
        rows = [_user_row(r) for r in raw_rows]

        if plan.where:
            rows = [r for r in rows if _eval_where(r, plan.where)]

        if plan.order_by:
            reverse = plan.order_dir.upper() == "DESC"
            rows = sorted(
                rows,
                key=lambda r: (r.get(plan.order_by) is None, r.get(plan.order_by)),
                reverse=reverse,
            )

        rows = rows[plan.offset:]
        if plan.limit is not None:
            rows = rows[:plan.limit]

        if plan.columns != "*":
            col_list = [c.strip() for c in plan.columns.split(",")]
            rows = [{c: r.get(c) for c in col_list} for r in rows]

        return rows
