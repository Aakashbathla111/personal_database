#!/usr/bin/env python3
"""
DBA Monitor Agent  —  agent/dba_monitor.py

An autonomous agent that uses Claude (claude-opus-4-8 with adaptive thinking)
to inspect a live MiniDB instance, identify health issues, and recommend (or
automatically apply) fixes.

The agent exposes five MiniDB operations as Claude tools:

  • get_table_stats   — row counts, column info for all/one table
  • check_wal_health  — WAL file size, record count, un-committed records
  • list_indexes      — which tables have indexes and on which columns
  • run_query         — execute any SELECT and return the rows
  • create_index      — create a missing index (requires --auto-fix flag)

Claude drives the diagnostic loop: it calls tools, synthesises the findings,
and returns a structured health report.

Usage
─────
  # one-shot health check
  python agent/dba_monitor.py

  # auto-apply recommended index creation
  python agent/dba_monitor.py --auto-fix

  # run every N seconds in the foreground
  python agent/dba_monitor.py --interval 60

  # combine
  python agent/dba_monitor.py --interval 300 --auto-fix
───────────
"""

import os
import sys
import json
import time
import argparse
import textwrap

# Allow imports from the project root regardless of where the script is called
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from groq import Groq
from storage.engine    import StorageEngine
from parser.parser     import Parser, ParseError
from executor.executor import Executor


# ── Shared database objects ───────────────────────────────────────────────────

_engine   = StorageEngine()
_parser   = Parser()
_executor = Executor(_engine)

MODEL = "qwen/qwen3.8-27b"

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert DBA (Database Administrator) monitoring a MiniDB instance.
    MiniDB is a from-scratch Python relational database with B+ tree indexes,
    a Write-Ahead Log (WAL) for crash recovery, a buffer pool, and full
    transaction support.

    Your job:
    1. Gather data by calling the available tools.
    2. Identify health issues: oversized tables without indexes, WAL bloat,
       missing indexes on frequently-filtered columns, un-recovered WAL records.
    3. If create_index is available (auto-fix mode is on), create indexes you
       deem essential.
    4. Return a concise, structured health report with:
         • SUMMARY   — one-paragraph overall assessment
         • ISSUES    — bullet list of problems found (severity: LOW/MEDIUM/HIGH)
         • ACTIONS   — what you did or recommend doing
         • METRICS   — key numbers (table sizes, WAL records, index coverage)

    Be thorough but concise. Call tools in a logical order: start with table
    stats, then WAL health, then indexes. Run targeted queries if you need
    more data. Only call create_index if the tool is available in your toolset.
""")


# ── Tool implementations ──────────────────────────────────────────────────────

def get_table_stats(table_name: str | None = None) -> dict:
    """Return row counts and column information for all tables (or one table)."""
    tables = _engine.tables
    if not tables:
        return {"tables": [], "message": "No tables found in the database."}

    targets = [table_name] if table_name else list(tables.keys())
    result  = []

    for name in targets:
        if name not in tables:
            result.append({"table": name, "error": "Table not found"})
            continue
        tbl     = tables[name]
        columns = [
            {
                "name":     col.name,
                "type":     col.datatype,
                "nullable": col.nullable,
                "default":  col.default,
            }
            for col in tbl.columns
        ]
        result.append({
            "table":        name,
            "row_count":    len(tbl.rows),
            "column_count": len(columns),
            "columns":      columns,
            "primary_key":  next((c.name for c in tbl.columns if c.primary_key), None),
        })

    return {"tables": result}


def check_wal_health() -> dict:
    """Return WAL file size, total record count, and uncommitted record count."""
    wal      = _engine.wal
    wal_file = "data/wal.log"

    if not os.path.exists(wal_file):
        return {
            "wal_exists":          False,
            "wal_size_bytes":      0,
            "total_records":       0,
            "uncommitted_records": 0,
            "message":             "WAL file does not exist (database is clean).",
        }

    size    = os.path.getsize(wal_file)
    records = wal.read_all()
    committed_txn_ids = wal.committed_txn_ids()

    uncommitted = [
        r for r in records
        if r.get("txn_id", 0) != 0 and r["txn_id"] not in committed_txn_ids
        and r.get("operation") not in ("COMMIT", "ROLLBACK", "BEGIN")
    ]

    return {
        "wal_exists":          True,
        "wal_size_bytes":      size,
        "wal_size_kb":         round(size / 1024, 2),
        "total_records":       len(records),
        "committed_txn_ids":   len(committed_txn_ids),
        "uncommitted_records": len(uncommitted),
        "needs_recovery":      len(uncommitted) > 0,
        "message":             (
            "WAL has uncommitted records — consider running RECOVER."
            if uncommitted else
            "WAL is healthy (all transactions committed or rolled back)."
        ),
    }


def list_indexes(table_name: str | None = None) -> dict:
    """Return index information for all tables (or one table)."""
    tables  = _engine.tables
    targets = [table_name] if table_name else list(tables.keys())
    result  = []

    for name in targets:
        if name not in tables:
            result.append({"table": name, "error": "Table not found"})
            continue
        tbl     = tables[name]
        indexes = []
        for idx_name, idx in tbl.indexes.items():
            indexes.append({
                "index_name": idx_name,
                "column":     idx.column_name,
                "unique":     getattr(idx, "unique", False),
                "type":       "B+ Tree",
            })
        result.append({
            "table":        name,
            "row_count":    len(tbl.rows),
            "index_count":  len(indexes),
            "indexes":      indexes,
            "has_pk_index": any(c.primary_key for c in tbl.columns),
        })

    return {"index_info": result}


def run_query(sql: str) -> dict:
    """Execute a SELECT query and return the rows (read-only; INSERT/UPDATE/DELETE blocked)."""
    upper = sql.strip().upper()
    if not upper.startswith("SELECT") and not upper.startswith("SHOW") and not upper.startswith("DESCRIBE"):
        return {"error": "Only SELECT / SHOW / DESCRIBE queries are allowed via run_query."}
    try:
        node   = _parser.parse(sql)
        result = _executor.execute(node)
        return {
            "rows":    result.get("rows", []),
            "message": result.get("message"),
            "count":   len(result.get("rows", [])),
        }
    except (ParseError, KeyError, ValueError, TypeError, RuntimeError) as exc:
        return {"error": str(exc)}


def create_index(table: str, column: str, unique: bool = False) -> dict:
    """Create a B+ tree index on table.column."""
    unique_kw = "UNIQUE " if unique else ""
    idx_name  = f"idx_{table}_{column}"
    sql       = f"CREATE {unique_kw}INDEX {idx_name} ON {table}({column})"
    try:
        node   = _parser.parse(sql)
        result = _executor.execute(node)
        return {
            "success":    True,
            "index_name": idx_name,
            "table":      table,
            "column":     column,
            "unique":     unique,
            "message":    result.get("message", "Index created."),
        }
    except (ParseError, KeyError, ValueError, TypeError, RuntimeError) as exc:
        return {"success": False, "error": str(exc)}


# ── Claude tool definitions ───────────────────────────────────────────────────

BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name":        "get_table_stats",
            "description": (
                "Return row counts and column info for all tables or a specific table. "
                "Call with no arguments to get all tables; pass table_name to narrow down."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type":        "string",
                        "description": "Optional. Name of the table to inspect. Omit for all tables.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name":        "check_wal_health",
            "description": (
                "Return WAL (Write-Ahead Log) health: file size, total records, "
                "uncommitted record count, and whether recovery is needed."
            ),
            "parameters": {
                "type":       "object",
                "properties": {},
                "required":   [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name":        "list_indexes",
            "description": (
                "Return index information (index name, column, type) for all tables "
                "or a specific table. Identifies tables with no indexes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type":        "string",
                        "description": "Optional. Narrow index listing to this table.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name":        "run_query",
            "description": (
                "Execute a read-only SQL query (SELECT / SHOW / DESCRIBE) against "
                "the live database and return the result rows. Use this for ad-hoc "
                "diagnostic queries when other tools are insufficient."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type":        "string",
                        "description": "SQL statement to execute. Must be SELECT, SHOW, or DESCRIBE.",
                    },
                },
                "required": ["sql"],
            },
        },
    },
]

CREATE_INDEX_TOOL = {
    "type": "function",
    "function": {
        "name":        "create_index",
        "description": (
            "Create a B+ tree index on a table column. Only call this when you have "
            "confirmed the table exists and lacks an index on the target column. "
            "Prefer unique=true for primary-key-like columns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type":        "string",
                    "description": "Name of the table.",
                },
                "column": {
                    "type":        "string",
                    "description": "Name of the column to index.",
                },
                "unique": {
                    "type":        "boolean",
                    "description": "True if all values in the column are unique.",
                },
            },
            "required": ["table", "column"],
        },
    },
}


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def dispatch_tool(name: str, args: dict) -> str:
    """Call the appropriate Python function and return the JSON result."""
    if name == "get_table_stats":
        result = get_table_stats(args.get("table_name"))
    elif name == "check_wal_health":
        result = check_wal_health()
    elif name == "list_indexes":
        result = list_indexes(args.get("table_name"))
    elif name == "run_query":
        result = run_query(args["sql"])
    elif name == "create_index":
        result = create_index(args["table"], args["column"], args.get("unique", False))
    else:
        result = {"error": f"Unknown tool: {name}"}
    return json.dumps(result, default=str)


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run_monitor(auto_fix: bool = False, verbose: bool = False) -> str:
    """
    Run one full DBA monitoring cycle.

    Sends a system prompt + tool definitions to the LLM, then drives the tool-use
    loop until it returns a final text response (the health report).
    """
    client = Groq()

    tools = BASE_TOOLS + ([CREATE_INDEX_TOOL] if auto_fix else [])

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role":    "user",
            "content": (
                "Please run a complete health check on this MiniDB database. "
                "Gather all available data, identify any issues, "
                + ("and create any missing indexes you recommend. " if auto_fix else "")
                + "Then produce your structured health report."
            ),
        }
    ]

    iteration = 0
    while True:
        iteration += 1
        if verbose:
            print(f"\n[agent] → API call #{iteration} ({len(messages)} messages in context)")

        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=8192,
            tools=tools,
            messages=messages,
        )

        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        if verbose:
            print(f"[agent] ← finish_reason={finish_reason}")

        # Accumulate assistant turn
        messages.append(msg)

        if finish_reason == "stop":
            return msg.content or "(No text in final response)"

        if finish_reason != "tool_calls":
            return f"(Unexpected finish_reason: {finish_reason})"

        # Execute every tool call and collect results
        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            if verbose:
                print(f"[agent]   tool_call: {name}({json.dumps(args)})")

            result_json = dispatch_tool(name, args)

            if verbose:
                preview = result_json[:200] + ("…" if len(result_json) > 200 else "")
                print(f"[agent]   tool_result: {preview}")

            messages.append({
                "role":         "tool",
                "tool_call_id": tool_call.id,
                "content":      result_json,
            })


# ── Pretty banner ─────────────────────────────────────────────────────────────

def _banner(run_no: int):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*64}")
    print(f"  MiniDB DBA Monitor — Run #{run_no}   {ts}")
    print(f"{'='*64}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="MiniDB DBA Monitor — Claude-powered autonomous health checker"
    )
    ap.add_argument(
        "--auto-fix",
        action="store_true",
        help="Allow the agent to create missing indexes automatically.",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Repeat the health check every N seconds (0 = run once and exit).",
    )
    ap.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each API call and tool invocation.",
    )
    args = ap.parse_args()

    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY is not set.", file=sys.stderr)
        print("  export GROQ_API_KEY=gsk_...", file=sys.stderr)
        sys.exit(1)

    run_no = 0
    while True:
        run_no += 1
        _banner(run_no)

        try:
            report = run_monitor(auto_fix=args.auto_fix, verbose=args.verbose)
            print(report)
        except Exception as exc:
            print(f"[ERROR] Anthropic API error: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\n[agent] Stopped by user.")
            break

        if args.interval <= 0:
            break

        print(f"\n[agent] Next check in {args.interval}s  (Ctrl-C to stop)")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[agent] Stopped by user.")
            break


if __name__ == "__main__":
    main()
