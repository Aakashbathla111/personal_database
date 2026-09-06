"""
AST node dataclasses — one per SQL statement type.

The Lexer produces tokens, the Parser consumes tokens and returns
one of these nodes, the Executor/Planner acts on the node.
"""

from dataclasses import dataclass, field
from typing import Any


# ── DML ──────────────────────────────────────────────────────────────────────

@dataclass
class SelectNode:
    table:     str
    columns:   str   = "*"
    where:     str   = None
    order_by:  str   = None
    order_dir: str   = "ASC"
    limit:     int   = None
    offset:    int   = 0
    group_by:  str   = None
    having:    str   = None
    join:      dict  = None    # {type, right_table, on_left, on_right}
    distinct:  bool  = False
    aliases:   dict  = None    # {alias: original_col} for SELECT col AS alias


@dataclass
class InsertNode:
    table:      str
    columns:    list = None       # None → positional
    values:     list = field(default_factory=list)
    multi_rows: list = None       # list of value-lists for multi-row INSERT


@dataclass
class UpdateNode:
    table:       str
    assignments: dict = field(default_factory=dict)
    where:       str  = None


@dataclass
class DeleteNode:
    table: str
    where: str = None


# ── DDL ──────────────────────────────────────────────────────────────────────

@dataclass
class CreateTableNode:
    table:   str
    columns: list = field(default_factory=list)


@dataclass
class DropTableNode:
    table:     str
    if_exists: bool = False


@dataclass
class TruncateNode:
    table: str


@dataclass
class RenameTableNode:
    old: str
    new: str


@dataclass
class AlterAddColumnNode:
    table:  str
    column: Any


@dataclass
class AlterDropColumnNode:
    table:  str
    column: str


@dataclass
class AlterRenameColumnNode:
    table: str
    old:   str
    new:   str


# ── INDEX ─────────────────────────────────────────────────────────────────────

@dataclass
class CreateIndexNode:
    index:  str
    table:  str
    column: str | list = ""   # str for single, list for composite
    unique: bool = False


@dataclass
class DropIndexNode:
    index: str
    table: str


@dataclass
class ShowIndexesNode:
    table: str


# ── VIEW ──────────────────────────────────────────────────────────────────────

@dataclass
class CreateViewNode:
    view:       str
    base_table: str
    columns:    str  = "*"
    where:      str  = None


@dataclass
class DropViewNode:
    view:      str
    if_exists: bool = False


@dataclass
class ShowViewsNode:
    pass


# ── TRANSACTION ───────────────────────────────────────────────────────────────

@dataclass
class BeginNode:
    pass


@dataclass
class CommitNode:
    pass


@dataclass
class RollbackNode:
    pass


# Mod 3: Savepoint nodes
@dataclass
class SavepointNode:
    """SAVEPOINT <name>"""
    name: str


@dataclass
class RollbackToSavepointNode:
    """ROLLBACK TO [SAVEPOINT] <name>"""
    name: str


@dataclass
class ReleaseSavepointNode:
    """RELEASE [SAVEPOINT] <name>"""
    name: str


# ── INFO ──────────────────────────────────────────────────────────────────────

@dataclass
class ShowTablesNode:
    pass


@dataclass
class DescribeNode:
    table: str


@dataclass
class RecoverNode:
    pass


@dataclass
class VacuumNode:
    """VACUUM — remove dead MVCC row versions"""
    pass


@dataclass
class ExitNode:
    pass
