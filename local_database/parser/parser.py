"""
Parser — consumes a token list (from Lexer) and returns an AST node.

Pipeline:
    SQL text
      └─► Lexer.tokenize()  → tokens
            └─► Parser.parse()  → ASTNode
                  └─► Executor.execute()  → result

Modifications in this version
──────────────────────────────
  Mod 3  — SAVEPOINT, ROLLBACK TO [SAVEPOINT], RELEASE [SAVEPOINT]
  Mod 6  — CROSS JOIN (no ON clause required)
  Mod 4b — BETWEEN … AND … recognised in WHERE (lexer passes tokens
            through; table._eval_where does the actual rewrite, but the
            parser must not consume the embedded AND as a clause boundary)
"""

from parser.lexer import Lexer
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
from storage.column import Column


class ParseError(Exception):
    pass


class Parser:

    def __init__(self):
        self._lexer  = Lexer()
        self._tokens : list[str] = []
        self._pos    = 0

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def parse(self, sql: str):
        self._tokens = self._lexer.tokenize(sql)
        self._pos    = 0
        return self._statement()

    # ------------------------------------------------------------------ #
    # Token helpers
    # ------------------------------------------------------------------ #

    def _peek(self, offset=0) -> str:
        idx = self._pos + offset
        return self._tokens[idx].upper() if idx < len(self._tokens) else ""

    def _advance(self) -> str:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, *words) -> str:
        tok = self._advance()
        for w in words:
            if tok.upper() == w.upper():
                return tok
        raise ParseError(f"Expected {words}, got '{tok}'")

    def _match(self, *words) -> bool:
        if self._peek().upper() in [w.upper() for w in words]:
            self._advance()
            return True
        return False

    def _remaining(self) -> list[str]:
        return self._tokens[self._pos:]

    def _rest_as_str(self) -> str:
        s = " ".join(self._remaining())
        self._pos = len(self._tokens)
        return s

    # ------------------------------------------------------------------ #
    # Top-level dispatch
    # ------------------------------------------------------------------ #

    def _statement(self):
        kw = self._peek()

        if kw == "SELECT":                  return self._parse_select()
        if kw == "INSERT":                  return self._parse_insert()
        if kw == "UPDATE":                  return self._parse_update()
        if kw == "DELETE":                  return self._parse_delete()
        if kw == "CREATE":                  return self._parse_create()
        if kw == "DROP":                    return self._parse_drop()
        if kw == "ALTER":                   return self._parse_alter()
        if kw == "TRUNCATE":                return self._parse_truncate()
        if kw == "RENAME":                  return self._parse_rename_table()
        if kw == "BEGIN":                   self._advance(); return BeginNode()
        if kw == "COMMIT":                  self._advance(); return CommitNode()
        if kw == "ROLLBACK":                return self._parse_rollback()
        if kw == "SAVEPOINT":               return self._parse_savepoint()
        if kw == "RELEASE":                 return self._parse_release_savepoint()
        if kw == "SHOW":                    return self._parse_show()
        if kw in ("DESCRIBE", "DESC"):      return self._parse_describe()
        if kw == "RECOVER":                 self._advance(); return RecoverNode()
        if kw == "VACUUM":                  self._advance(); return VacuumNode()
        if kw in ("EXIT", "QUIT"):          self._advance(); return ExitNode()

        raise ParseError(f"Unknown statement starting with '{kw}'")

    # ------------------------------------------------------------------ #
    # SELECT
    # ------------------------------------------------------------------ #

    def _parse_select(self):
        self._expect("SELECT")
        node = SelectNode(table="")

        # DISTINCT
        if self._peek() == "DISTINCT":
            self._advance()
            node.distinct = True

        col_toks = []
        while self._peek() and self._peek() != "FROM":
            col_toks.append(self._advance())
        node.columns, node.aliases = self._reassemble_cols_with_aliases(col_toks)

        self._expect("FROM")
        node.table = self._advance()

        # optional JOIN (INNER / LEFT / RIGHT / FULL / CROSS)
        if self._peek() in ("JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS"):
            node.join = self._parse_join_clause()

        # optional WHERE
        if self._peek() == "WHERE":
            self._advance()
            node.where = self._collect_where_until("GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET")

        # optional GROUP BY
        if self._peek() == "GROUP":
            self._advance(); self._expect("BY")
            raw = self._collect_until("HAVING", "ORDER", "LIMIT", "OFFSET")
            node.group_by = self._reassemble_cols(raw.split())

        # optional HAVING
        if self._peek() == "HAVING":
            self._advance()
            node.having = self._collect_until("ORDER", "LIMIT", "OFFSET")

        # optional ORDER BY
        if self._peek() == "ORDER":
            self._advance(); self._expect("BY")
            node.order_by = self._advance()
            if self._peek() in ("ASC", "DESC"):
                node.order_dir = self._advance().upper()

        # optional LIMIT
        if self._peek() == "LIMIT":
            self._advance()
            node.limit = int(self._advance())

        # optional OFFSET
        if self._peek() == "OFFSET":
            self._advance()
            node.offset = int(self._advance())

        return node

    def _parse_join_clause(self):
        join_type = "INNER"
        if self._peek() in ("INNER", "LEFT", "RIGHT", "FULL", "CROSS"):
            join_type = self._advance().upper()
        self._expect("JOIN")
        right_table = self._advance()

        # Mod 6: CROSS JOIN has no ON clause
        if join_type == "CROSS":
            return {
                "type":        "CROSS",
                "right_table": right_table,
                "on_left":     None,
                "on_right":    None,
            }

        self._expect("ON")
        on_left  = self._read_col_ref()
        self._expect("=")
        on_right = self._read_col_ref()
        return {
            "type":        join_type,
            "right_table": right_table,
            "on_left":     on_left,
            "on_right":    on_right,
        }

    def _read_col_ref(self) -> str:
        name = self._advance()
        if self._peek() == ".":
            self._advance()
            name = name + "." + self._advance()
        return name

    def _collect_until(self, *stop_words) -> str:
        """Collect tokens into a string until a stop keyword is seen."""
        toks = []
        while self._peek() and self._peek().upper() not in stop_words:
            toks.append(self._advance())
        return " ".join(toks).strip()

    def _collect_where_until(self, *stop_words) -> str:
        """
        Like _collect_until but aware of BETWEEN … AND … so the AND
        inside a BETWEEN clause is not mistaken for a clause boundary.

        Also handles subqueries: col IN (SELECT ...) keeps the inner
        SELECT together by tracking paren depth.
        """
        toks  = []
        depth = 0   # paren depth for subqueries
        i     = self._pos

        while i < len(self._tokens):
            tok   = self._tokens[i]
            upper = tok.upper()

            # stop only at depth-0 keywords
            if depth == 0 and upper in stop_words:
                break

            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1

            # skip the AND that is part of BETWEEN ... AND ...
            if (upper == "AND" and depth == 0
                    and len(toks) >= 3
                    and toks[-2].upper() == "BETWEEN"):
                toks.append(tok)
                i += 1
                continue

            toks.append(tok)
            i += 1

        self._pos = i
        return " ".join(toks).strip()

    # ------------------------------------------------------------------ #
    # INSERT
    # ------------------------------------------------------------------ #

    def _parse_insert(self):
        self._expect("INSERT")
        self._expect("INTO")
        table = self._advance()
        node  = InsertNode(table=table)

        if self._peek() == "(":
            self._advance()
            cols = []
            while self._peek() != ")":
                if self._peek() == ",":
                    self._advance()
                    continue
                cols.append(self._advance())
            self._advance()
            node.columns = cols

        self._expect("VALUES")
        self._expect("(")
        node.values = self._parse_value_list()
        self._expect(")")

        # Multi-row INSERT: VALUES (...), (...), ...
        if self._peek() == ",":
            node.multi_rows = [node.values]
            while self._peek() == ",":
                self._advance()  # consume ','
                self._expect("(")
                node.multi_rows.append(self._parse_value_list())
                self._expect(")")
            node.values = node.multi_rows[0]

        return node

    def _parse_value_list(self) -> list:
        values = []
        while self._peek() != ")":
            if self._peek() == ",":
                self._advance()
                continue
            raw = self._advance()
            if raw.upper() == "NULL":
                values.append(None)
            elif raw.startswith("'") and raw.endswith("'"):
                values.append(raw[1:-1])
            else:
                try:
                    values.append(int(raw))
                except ValueError:
                    try:
                        values.append(float(raw))
                    except ValueError:
                        values.append(raw)
        return values

    # ------------------------------------------------------------------ #
    # UPDATE
    # ------------------------------------------------------------------ #

    def _parse_update(self):
        self._expect("UPDATE")
        table = self._advance()
        self._expect("SET")
        node  = UpdateNode(table=table)

        while self._peek() and self._peek() != "WHERE":
            if self._peek() == ",":
                self._advance()
                continue
            col = self._advance()
            self._expect("=")
            val = self._advance()
            if val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            node.assignments[col] = val

        if self._peek() == "WHERE":
            self._advance()
            node.where = self._rest_as_str()

        return node

    # ------------------------------------------------------------------ #
    # DELETE
    # ------------------------------------------------------------------ #

    def _parse_delete(self):
        self._expect("DELETE")
        self._expect("FROM")
        table = self._advance()
        where = None
        if self._peek() == "WHERE":
            self._advance()
            where = self._rest_as_str()
        return DeleteNode(table=table, where=where)

    # ------------------------------------------------------------------ #
    # CREATE
    # ------------------------------------------------------------------ #

    def _parse_create(self):
        self._expect("CREATE")
        next_kw = self._peek().upper()

        if next_kw == "TABLE":    return self._parse_create_table()
        if next_kw in ("INDEX", "UNIQUE"): return self._parse_create_index()
        if next_kw == "VIEW":     return self._parse_create_view()

        raise ParseError(f"Unknown CREATE target: '{next_kw}'")

    def _parse_create_table(self):
        self._expect("TABLE")
        table = self._advance()
        self._expect("(")
        col_defs_raw = []
        depth = 1
        toks  = []
        while depth > 0 and self._pos < len(self._tokens):
            tok = self._advance()
            if tok == "(":
                depth += 1
                toks.append(tok)
            elif tok == ")":
                depth -= 1
                if depth > 0:
                    toks.append(tok)
            elif tok == "," and depth == 1:
                col_defs_raw.append(" ".join(toks))
                toks = []
            else:
                toks.append(tok)
        if toks:
            col_defs_raw.append(" ".join(toks))

        columns = [
            self._parse_column_def(d.strip())
            for d in col_defs_raw
            if d.strip() and not d.strip().upper().startswith("CHECK")
        ]
        return CreateTableNode(table=table, columns=columns)

    def _parse_column_def(self, defn: str) -> Column:
        import re
        tokens = defn.split()
        name  = tokens[0]
        dtype = tokens[1].upper()
        rest  = " ".join(tokens[2:]).upper()

        pk       = "PRIMARY KEY" in rest
        not_null = "NOT NULL" in rest or pk
        unique   = "UNIQUE" in rest or pk
        nullable = not not_null

        default = None
        dm = re.search(r"DEFAULT\s+([^\s]+)", defn, re.IGNORECASE)
        if dm:
            default = dm.group(1).strip("'\"")

        check = None
        cm = re.search(r"CHECK\s*\(([^)]+)\)", defn, re.IGNORECASE)
        if cm:
            check = cm.group(1).strip()

        fk = None
        fm = re.search(r"REFERENCES\s+(\w+)\s*\(\s*(\w+)\s*\)", defn, re.IGNORECASE)
        if fm:
            fk = {"table": fm.group(1), "column": fm.group(2)}

        return Column(
            name=name, datatype=dtype,
            primary_key=pk, nullable=nullable, unique=unique,
            default=default, check=check, foreign_key=fk,
        )

    def _parse_create_index(self):
        unique = False
        if self._peek() == "UNIQUE":
            self._advance()
            unique = True
        self._expect("INDEX")
        name = self._advance()
        self._expect("ON")
        table = self._advance()
        self._expect("(")
        # Parse one or more columns: (col1) or (col1, col2, col3)
        columns = [self._advance()]
        while self._peek() == ",":
            self._advance()  # skip comma
            columns.append(self._advance())
        self._expect(")")
        # Single column → str, multiple → list
        col = columns[0] if len(columns) == 1 else columns
        return CreateIndexNode(index=name, table=table, column=col, unique=unique)

    def _parse_create_view(self):
        self._expect("VIEW")
        view = self._advance()
        self._expect("AS")
        inner = self._statement()
        if not isinstance(inner, SelectNode):
            raise ParseError("CREATE VIEW … AS must be followed by a SELECT")
        return CreateViewNode(
            view=view,
            base_table=inner.table,
            columns=inner.columns,
            where=inner.where,
        )

    # ------------------------------------------------------------------ #
    # DROP
    # ------------------------------------------------------------------ #

    def _parse_drop(self):
        self._expect("DROP")
        kw = self._peek().upper()
        if kw == "TABLE":
            self._advance()
            if_exists = False
            if self._peek() == "IF":
                self._advance(); self._expect("EXISTS")
                if_exists = True
            return DropTableNode(table=self._advance(), if_exists=if_exists)
        if kw == "INDEX":
            self._advance()
            name = self._advance()
            self._expect("ON")
            table = self._advance()
            return DropIndexNode(index=name, table=table)
        if kw == "VIEW":
            self._advance()
            if_exists = False
            if self._peek() == "IF":
                self._advance(); self._expect("EXISTS")
                if_exists = True
            return DropViewNode(view=self._advance(), if_exists=if_exists)
        raise ParseError(f"Unknown DROP target: '{kw}'")

    # ------------------------------------------------------------------ #
    # ALTER
    # ------------------------------------------------------------------ #

    def _parse_alter(self):
        self._expect("ALTER")
        self._expect("TABLE")
        table = self._advance()
        op    = self._peek().upper()

        if op == "ADD":
            self._advance()
            self._match("COLUMN")
            col = self._parse_column_def(" ".join(self._remaining()))
            self._pos = len(self._tokens)
            return AlterAddColumnNode(table=table, column=col)

        if op == "DROP":
            self._advance()
            self._match("COLUMN")
            col = self._advance()
            return AlterDropColumnNode(table=table, column=col)

        if op == "RENAME":
            self._advance()
            self._match("COLUMN")
            old = self._advance()
            self._expect("TO")
            new = self._advance()
            return AlterRenameColumnNode(table=table, old=old, new=new)

        raise ParseError(f"Unknown ALTER TABLE operation: '{op}'")

    # ------------------------------------------------------------------ #
    # TRUNCATE / RENAME TABLE
    # ------------------------------------------------------------------ #

    def _parse_truncate(self):
        self._expect("TRUNCATE")
        self._match("TABLE")
        return TruncateNode(table=self._advance())

    def _parse_rename_table(self):
        self._expect("RENAME")
        self._expect("TABLE")
        old = self._advance()
        self._expect("TO")
        new = self._advance()
        return RenameTableNode(old=old, new=new)

    # ------------------------------------------------------------------ #
    # Mod 3 — ROLLBACK (plain or TO SAVEPOINT)
    # ------------------------------------------------------------------ #

    def _parse_rollback(self):
        self._advance()  # consume ROLLBACK
        # ROLLBACK TO [SAVEPOINT] <name>
        if self._peek() == "TO":
            self._advance()
            self._match("SAVEPOINT")
            name = self._advance()
            return RollbackToSavepointNode(name=name)
        return RollbackNode()

    # ------------------------------------------------------------------ #
    # Mod 3 — SAVEPOINT <name>
    # ------------------------------------------------------------------ #

    def _parse_savepoint(self):
        self._expect("SAVEPOINT")
        name = self._advance()
        return SavepointNode(name=name)

    # ------------------------------------------------------------------ #
    # Mod 3 — RELEASE [SAVEPOINT] <name>
    # ------------------------------------------------------------------ #

    def _parse_release_savepoint(self):
        self._expect("RELEASE")
        self._match("SAVEPOINT")
        name = self._advance()
        return ReleaseSavepointNode(name=name)

    # ------------------------------------------------------------------ #
    # SHOW / DESCRIBE
    # ------------------------------------------------------------------ #

    def _parse_show(self):
        self._expect("SHOW")
        kw = self._peek().upper()
        if kw == "TABLES":
            self._advance(); return ShowTablesNode()
        if kw == "VIEWS":
            self._advance(); return ShowViewsNode()
        if kw == "INDEXES":
            self._advance(); self._expect("ON")
            return ShowIndexesNode(table=self._advance())
        raise ParseError(f"Unknown SHOW target: '{kw}'")

    def _parse_describe(self):
        self._advance()
        return DescribeNode(table=self._advance())

    # ------------------------------------------------------------------ #
    # Token re-assembly helpers
    # ------------------------------------------------------------------ #

    def _reassemble_cols_with_aliases(self, toks: list[str]) -> tuple[str, dict]:
        """
        Reassemble column tokens and extract AS aliases.
        Returns (columns_str, aliases_dict).
        aliases_dict maps alias → original expression, e.g. {"n": "name"}.
        """
        if not toks:
            return "*", None

        # First pass: split tokens by comma at depth 0 to get per-column groups
        groups = []
        current = []
        depth = 0
        for tok in toks:
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
            if tok == "," and depth == 0:
                groups.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            groups.append(current)

        aliases = {}
        col_strs = []
        for group in groups:
            # Check for AS alias: [..., AS, alias_name]
            as_idx = None
            for i, tok in enumerate(group):
                if tok.upper() == "AS" and i > 0 and i == len(group) - 2:
                    as_idx = i
                    break
            if as_idx is not None:
                expr_toks = group[:as_idx]
                alias = group[as_idx + 1]
                expr = self._reassemble_cols(expr_toks)
                aliases[alias] = expr
                col_strs.append(expr)
            else:
                col_strs.append(self._reassemble_cols(group))

        return ", ".join(col_strs), aliases if aliases else None

    def _reassemble_cols(self, toks: list[str]) -> str:
        if not toks:
            return "*"
        out  = []
        glue = False
        for tok in toks:
            if glue:
                out.append(tok)
                glue = False
            elif tok in ("(", ")"):
                if out and out[-1] not in (",", " "):
                    out.append(tok)
                else:
                    out.append(tok)
                glue = (tok == "(")
            elif tok == ".":
                out.append(tok)
                glue = True
            elif tok == ",":
                out.append(", ")
            else:
                if out and not out[-1].endswith((" ", "(")):
                    out.append(" ")
                out.append(tok)
        return "".join(out).strip()
