#!/usr/bin/env python3
"""
Natural Language to SQL  —  agent/nl_to_sql.py

Takes a human-readable question (e.g. "show me all users older than 25")
and converts it to a SQL query using the LLM, then executes it against MiniDB.

Usage
─────
  python agent/nl_to_sql.py "show me all users older than 25"
  python agent/nl_to_sql.py   # interactive mode

Environment
───────────
  GROQ_API_KEY   — required; set in your shell or .env file
"""

import os
import sys
import json
import argparse
import textwrap

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


def _get_schema_description() -> str:
    """Build a text description of all tables and their columns."""
    tables = _engine.tables
    if not tables:
        return "No tables exist in the database."

    parts = []
    for name, tbl in tables.items():
        cols = []
        for c in tbl.columns:
            col_desc = f"  - {c.name} ({c.datatype}"
            if c.primary_key:
                col_desc += ", PRIMARY KEY"
            if not c.nullable:
                col_desc += ", NOT NULL"
            if c.default is not None:
                col_desc += f", DEFAULT {c.default}"
            col_desc += ")"
            cols.append(col_desc)

        # Sample rows for context
        sample_rows = ""
        if tbl.rows:
            sample = tbl.rows[:3]
            sample_rows = f"\n  Sample data ({len(tbl.rows)} total rows):\n"
            for row in sample:
                sample_rows += f"    {row}\n"

        parts.append(f"Table: {name}\n" + "\n".join(cols) + sample_rows)

    return "\n\n".join(parts)


SYSTEM_PROMPT = textwrap.dedent("""\
    You are a SQL query generator for MiniDB, a Python relational database.

    The user will ask a question in plain English. Your job is to:
    1. Understand what data they want.
    2. Generate a valid SQL query that answers their question.
    3. Return ONLY the SQL query, nothing else. No explanation, no markdown,
       no code fences, no backticks — just the raw SQL.

    MiniDB supports:
      • Data types: INT, TEXT, FLOAT, BOOL, DATE, BLOB (ONLY these — no DECIMAL,
        VARCHAR, CHAR, BIGINT, etc. Use FLOAT for money/salary, TEXT for strings)
      • SELECT, INSERT, UPDATE, DELETE
      • WHERE with =, !=, <, >, <=, >=, BETWEEN, IN, LIKE, IS NULL, IS NOT NULL
      • JOIN (INNER JOIN, LEFT JOIN) with ON
      • GROUP BY, HAVING, ORDER BY, LIMIT
      • COUNT, SUM, AVG, MIN, MAX aggregate functions
      • CREATE TABLE, DROP TABLE, SHOW TABLES, DESCRIBE
      • CREATE INDEX

    Rules:
      • Always use the exact table and column names from the schema.
      • The user MUST mention or clearly refer to a specific table or column
        from the schema. If the question is vague, generic, or does not
        reference any table/column (e.g. "hello", "show me data", "get results"),
        respond with: ERROR: Please mention a specific table or column name in your question.
      • If the question cannot be answered with the available tables, say:
        ERROR: <brief explanation>
      • Do NOT guess or assume which table the user means if they haven't
        indicated it. Ask them to be specific.
      • Do NOT wrap the SQL in quotes or code blocks.
""")


def run_nl_to_sql(question: str, auto_run: bool = True, verbose: bool = False) -> dict:
    """
    Convert a natural language question to SQL and optionally execute it.

    Returns a dict with:
      - sql: the generated SQL query
      - rows: query results (if auto_run and it's a SELECT)
      - message: status message
      - error: error string if something went wrong
    """
    client = Groq()

    schema = _get_schema_description()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Database schema:\n\n{schema}\n\n"
                f"Question: {question}"
            ),
        },
    ]

    if verbose:
        print(f"[nl_to_sql] Sending question to LLM: {question}")

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        temperature=0,
        messages=messages,
    )

    sql = response.choices[0].message.content.strip()

    # Strip any markdown fences the model might add despite instructions
    if sql.startswith("```"):
        lines = sql.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        sql = "\n".join(lines).strip()

    if verbose:
        print(f"[nl_to_sql] Generated SQL: {sql}")

    if sql.upper().startswith("ERROR:"):
        return {"sql": None, "error": sql}

    result = {"sql": sql}

    # Only auto-run read-only queries; write queries are shown but not executed
    first_word = sql.strip().split()[0].upper() if sql.strip() else ""
    if first_word not in ("SELECT", "SHOW", "DESCRIBE"):
        result["message"] = "Query generated. Copy it to the SQL Query tab to execute."
        return result

    if auto_run:
        try:
            node = _parser.parse(sql.rstrip(";"))
            exec_result = _executor.execute(node)
            result["rows"] = exec_result.get("rows")
            result["message"] = exec_result.get("message")
            result["list"] = exec_result.get("list")
        except (ParseError, KeyError, ValueError, TypeError, RuntimeError) as exc:
            result["error"] = str(exc)

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="MiniDB Natural Language to SQL — ask questions in plain English"
    )
    ap.add_argument(
        "question",
        nargs="?",
        help="Question in plain English. Omit for interactive mode.",
    )
    ap.add_argument(
        "--no-run",
        action="store_true",
        help="Only generate SQL, don't execute it.",
    )
    ap.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print debug info.",
    )
    args = ap.parse_args()

    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY is not set.", file=sys.stderr)
        print("  export GROQ_API_KEY=gsk_...", file=sys.stderr)
        sys.exit(1)

    question = args.question
    if not question:
        print("MiniDB Natural Language to SQL (Ctrl-C to quit)")
        try:
            question = input("\nAsk a question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

    if not question:
        print("No question provided.")
        sys.exit(1)

    result = run_nl_to_sql(question, auto_run=not args.no_run, verbose=args.verbose)

    if result.get("sql"):
        print(f"\nGenerated SQL:\n  {result['sql']}\n")

    if result.get("error"):
        print(f"Error: {result['error']}")
    elif result.get("rows"):
        for row in result["rows"]:
            print(row)
        print(f"\n({len(result['rows'])} rows)")
    elif result.get("message"):
        print(result["message"])
    elif result.get("list"):
        for item in result["list"]:
            print(f"  - {item}")


if __name__ == "__main__":
    main()
