#!/usr/bin/env python3
"""
Query Optimizer Agent  —  agent/query_optimizer.py

An agentic workflow powered by Claude (claude-opus-4-8 with adaptive thinking)
that takes a SQL query, analyses its execution plan, benchmarks it, discovers
missing indexes, rewrites the query for efficiency, and produces a before/after
performance report.

Workflow the agent drives autonomously:
  1. explain_query     — inspect the current execution plan
  2. get_table_stats   — understand table sizes and column types
  3. list_indexes      — see what indexes already exist
  4. benchmark_query   — measure baseline execution time
  5. create_index      — add missing indexes (if --auto-fix)
  6. explain_query     — verify plan improved
  7. benchmark_query   — measure optimized execution time
  → Final report: original vs rewritten query, plan diff, timing comparison

Usage
─────
  python agent/query_optimizer.py "SELECT * FROM orders WHERE customer_id = 5"

  # Allow the agent to create indexes automatically
  python agent/query_optimizer.py --auto-fix "SELECT * FROM orders WHERE ..."

  # Show every API call and tool invocation
  python agent/query_optimizer.py -v "SELECT * FROM orders WHERE ..."

  # Interactive mode (prompt for query)
  python agent/query_optimizer.py

"""

import os
import sys
import json
import time
import argparse
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from groq import Groq
from storage.engine    import StorageEngine
from parser.parser     import Parser, ParseError
from executor.executor import Executor
from executor.planner  import QueryPlanner


# ── Shared database objects ───────────────────────────────────────────────────

_engine   = StorageEngine()
_parser   = Parser()
_executor = Executor(_engine)
_planner  = QueryPlanner()

MODEL = "qwen/qwen3.8-27b"

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert database query optimizer for MiniDB, a Python relational
    database that supports:
      • B+ tree indexes (exact-match IndexScanPlan and range RangeScanPlan)
      • Full table scans (FullScanPlan) when no usable index exists
      • JOINs (hash-join on indexed FK columns), GROUP BY, HAVING, ORDER BY
      • BETWEEN, IN, subqueries, LIKE, IS NULL / IS NOT NULL

    Your job:
    1. Call explain_query on the original SQL to see the current plan.
    2. Call get_table_stats and list_indexes for relevant tables.
    3. Call benchmark_query to measure baseline performance.
    4. Identify inefficiencies:
         - FullScanPlan on large tables (> 100 rows) that filter on a column
         - Missing indexes on WHERE / JOIN columns
         - SELECT * when only a few columns are needed
         - Avoidable full scans caused by non-sargable predicates (e.g. LIKE '%x')
    5. If create_index is available, add indexes you deem essential.
    6. Propose a rewritten SQL if the structure can be improved (e.g. swap
       JOIN order, add explicit column list, use BETWEEN instead of >= AND <=).
    7. Call explain_query and benchmark_query on the optimised SQL.
    8. Return a structured report with these sections:
         ORIGINAL QUERY    — the input SQL
         ORIGINAL PLAN     — plan type, estimated cost
         OPTIMISED QUERY   — rewritten SQL (or "no rewrite needed")
         OPTIMISED PLAN    — plan type after changes
         INDEXES CREATED   — list of indexes added (or "none")
         TIMING            — before vs after (ms), speedup factor
         RECOMMENDATIONS   — bullet list of what was done and why

    If the query is already optimal, say so clearly.
    Only call create_index when the tool is listed in your available tools.
""")


# ── Tool implementations ──────────────────────────────────────────────────────

def explain_query(sql: str) -> dict:
    """
    Parse and plan a SELECT query, returning the query plan details.
    Does NOT execute the query.
    """
    upper = sql.strip().upper()
    if not upper.startswith("SELECT"):
        return {"error": "explain_query only supports SELECT statements."}
    try:
        node = _parser.parse(sql)
        # Pull the plan directly from the planner without executing rows
        from parser.ast import SelectNode
        if not isinstance(node, SelectNode):
            return {"error": "Not a SELECT statement."}
        plan = _planner.plan(node, _engine)
        explain = plan.explain() if hasattr(plan, "explain") else {}
        return {
            "sql":        sql,
            "plan_type":  type(plan).__name__,
            "plan_note":  getattr(plan, "note", ""),
            "explain":    explain,
        }
    except (ParseError, KeyError, ValueError, TypeError, RuntimeError) as exc:
        return {"error": str(exc)}


def benchmark_query(sql: str, runs: int = 5) -> dict:
    """
    Execute a SELECT query `runs` times and return timing statistics (ms).
    Returns avg, min, max execution times and the row count.
    """
    upper = sql.strip().upper()
    if not upper.startswith("SELECT"):
        return {"error": "benchmark_query only supports SELECT statements."}

    times = []
    row_count = 0
    error     = None

    for i in range(runs):
        try:
            t0     = time.perf_counter()
            node   = _parser.parse(sql)
            result = _executor.execute(node)
            t1     = time.perf_counter()
            times.append((t1 - t0) * 1000)
            if i == 0:
                row_count = len(result.get("rows", []))
        except (ParseError, KeyError, ValueError, TypeError, RuntimeError) as exc:
            error = str(exc)
            break

    if error:
        return {"error": error}

    return {
        "sql":       sql,
        "runs":      runs,
        "row_count": row_count,
        "avg_ms":    round(sum(times) / len(times), 4),
        "min_ms":    round(min(times), 4),
        "max_ms":    round(max(times), 4),
        "all_ms":    [round(t, 4) for t in times],
    }


def get_table_stats(table_name: str | None = None) -> dict:
    """Return row counts and column information for all tables or one table."""
    tables  = _engine.tables
    if not tables:
        return {"tables": [], "message": "No tables found."}

    targets = [table_name] if table_name else list(tables.keys())
    result  = []
    for name in targets:
        if name not in tables:
            result.append({"table": name, "error": "Not found"})
            continue
        tbl = tables[name]
        result.append({
            "table":       name,
            "row_count":   len(tbl.rows),
            "columns":     [
                {"name": c.name, "type": c.datatype, "nullable": c.nullable}
                for c in tbl.columns
            ],
            "primary_key": next((c.name for c in tbl.columns if c.primary_key), None),
        })
    return {"tables": result}


def list_indexes(table_name: str | None = None) -> dict:
    """Return index information for all tables or one table."""
    tables  = _engine.tables
    targets = [table_name] if table_name else list(tables.keys())
    result  = []
    for name in targets:
        if name not in tables:
            result.append({"table": name, "error": "Not found"})
            continue
        tbl = tables[name]
        result.append({
            "table":      name,
            "row_count":  len(tbl.rows),
            "indexes":    [
                {"index_name": k, "column": v.column_name, "unique": v.unique}
                for k, v in tbl.indexes.items()
            ],
        })
    return {"index_info": result}


def create_index(table: str, column: str, unique: bool = False) -> dict:
    """Create a B+ tree index on table.column."""
    idx_name = f"idx_{table}_{column}"
    unique_kw = "UNIQUE " if unique else ""
    sql = f"CREATE {unique_kw}INDEX {idx_name} ON {table}({column})"
    try:
        result = _executor.execute(_parser.parse(sql))
        return {
            "success":    True,
            "index_name": idx_name,
            "table":      table,
            "column":     column,
            "message":    result.get("message", "Index created."),
        }
    except (ParseError, KeyError, ValueError, TypeError, RuntimeError) as exc:
        return {"success": False, "error": str(exc)}


# ── Claude tool definitions ───────────────────────────────────────────────────

BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name":        "explain_query",
            "description": (
                "Parse and plan a SELECT query without executing it. "
                "Returns plan type (FullScanPlan / IndexScanPlan / RangeScanPlan) "
                "and structured EXPLAIN output. Call this first on the original query "
                "and again on any rewritten version to compare plans."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SELECT statement to explain."},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name":        "benchmark_query",
            "description": (
                "Execute a SELECT query multiple times and return avg/min/max timing "
                "in milliseconds plus the result row count. Use this to measure "
                "baseline performance and then again after optimisation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql":  {"type": "string",  "description": "SELECT statement to benchmark."},
                    "runs": {
                        "type":        "integer",
                        "description": "Number of executions (default 5).",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name":        "get_table_stats",
            "description": "Return row counts and column info for all or one table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type":        "string",
                        "description": "Optional. Omit to get all tables.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name":        "list_indexes",
            "description": "Return index information (name, column, unique) for all or one table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type":        "string",
                        "description": "Optional. Omit to get all tables.",
                    },
                },
                "required": [],
            },
        },
    },
]

CREATE_INDEX_TOOL = {
    "type": "function",
    "function": {
        "name":        "create_index",
        "description": (
            "Create a B+ tree index on a column. Only call this when the tool is "
            "available (auto-fix mode). Call explain_query and benchmark_query "
            "afterwards to verify the plan improved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table":  {"type": "string",  "description": "Table name."},
                "column": {"type": "string",  "description": "Column to index."},
                "unique": {"type": "boolean", "description": "True if values are unique."},
            },
            "required": ["table", "column"],
        },
    },
}


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def dispatch_tool(name: str, args: dict) -> str:
    if name == "explain_query":
        result = explain_query(args["sql"])
    elif name == "benchmark_query":
        result = benchmark_query(args["sql"], args.get("runs", 5))
    elif name == "get_table_stats":
        result = get_table_stats(args.get("table_name"))
    elif name == "list_indexes":
        result = list_indexes(args.get("table_name"))
    elif name == "create_index":
        result = create_index(args["table"], args["column"], args.get("unique", False))
    else:
        result = {"error": f"Unknown tool: {name}"}
    return json.dumps(result, default=str)


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run_optimizer(query: str, auto_fix: bool = False, verbose: bool = False) -> str:
    """
    Run one full query-optimisation cycle for the given SQL.
    Returns the agent's final text report.
    """
    client = Groq()
    tools  = BASE_TOOLS + ([CREATE_INDEX_TOOL] if auto_fix else [])

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role":    "user",
            "content": (
                f"Please optimise this MiniDB query:\n\n```sql\n{query}\n```\n\n"
                + ("You may create indexes if needed. " if auto_fix else
                   "Do not create indexes; only recommend them. ")
                + "Produce a full optimisation report."
            ),
        }
    ]

    iteration = 0
    while True:
        iteration += 1
        if verbose:
            print(f"\n[optimizer] → API call #{iteration}")

        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=8192,
            tools=tools,
            messages=messages,
        )

        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        if verbose:
            print(f"[optimizer] ← finish_reason={finish_reason}")

        messages.append(msg)

        if finish_reason == "stop":
            return msg.content or "(No text in final response)"

        if finish_reason != "tool_calls":
            return f"(Unexpected finish_reason: {finish_reason})"

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            if verbose:
                print(f"[optimizer]   tool: {name}({json.dumps(args)})")

            result_json = dispatch_tool(name, args)

            if verbose:
                preview = result_json[:300] + ("…" if len(result_json) > 300 else "")
                print(f"[optimizer]   result: {preview}")

            messages.append({
                "role":         "tool",
                "tool_call_id": tool_call.id,
                "content":      result_json,
            })


# ── Entry point ───────────────────────────────────────────────────────────────

def _print_divider():
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser(
        description="MiniDB Query Optimizer — Claude-powered query analysis and rewrite"
    )
    ap.add_argument(
        "query",
        nargs="?",
        help="SQL SELECT query to optimise. Omit for interactive mode.",
    )
    ap.add_argument(
        "--auto-fix",
        action="store_true",
        help="Allow the agent to create missing indexes automatically.",
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

    query = args.query
    if not query:
        print("MiniDB Query Optimizer (Ctrl-C to quit)")
        print("Enter a SELECT query to optimise:\n")
        try:
            lines = []
            while True:
                line = input("sql> " if not lines else "  -> ")
                lines.append(line.strip())
                if line.rstrip().endswith(";") or (lines and not line.strip()):
                    break
            query = " ".join(l for l in lines if l).rstrip(";")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

    if not query.strip():
        print("No query provided.")
        sys.exit(1)

    _print_divider()
    print("  MiniDB Query Optimizer")
    print(f"  Query : {query[:60]}{'…' if len(query) > 60 else ''}")
    print(f"  Mode  : {'auto-fix ON' if args.auto_fix else 'recommend only'}")
    _print_divider()
    print()

    try:
        report = run_optimizer(query, auto_fix=args.auto_fix, verbose=args.verbose)
        print(report)
    except Exception as exc:
        print(f"[ERROR] Anthropic API error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
