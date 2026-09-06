#!/usr/bin/env python3
"""
MiniDB TCP Database Server  —  db_server.py

A standalone database server that accepts SQL queries AND agent commands
over TCP. Single process = single engine = consistent MVCC state.

Protocol (newline-delimited JSON):
  SQL query:     {"sql": "SELECT * FROM employees"}
  DBA Monitor:   {"cmd": "dba-monitor", "auto_fix": false}
  NL to SQL:     {"cmd": "ask", "question": "show me all employees"}
  Optimizer:     {"cmd": "query-optimizer", "sql": "SELECT ...", "auto_fix": false}

Usage:
  python db_server.py [--host HOST] [--port PORT]
"""

import os
import sys
import json
import socket
import argparse
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from storage.engine    import StorageEngine
from parser.parser     import Parser, ParseError
from executor.executor import Executor

# ── Single shared engine ─────────────────────────────────────────────────

_engine = StorageEngine()
print(f"[MiniDB] Loaded {len(_engine.tables)} table(s): {list(_engine.tables.keys())}")

# ── Load agents (wired to the SAME engine) ───────────────────────────────

_HAS_AGENTS = False
try:
    import agent.dba_monitor as _dba_mod
    import agent.nl_to_sql as _nl_mod
    import agent.query_optimizer as _opt_mod

    _dba_mod._engine   = _engine
    _dba_mod._parser   = Parser()
    _dba_mod._executor = Executor(_engine)

    _nl_mod._engine   = _engine
    _nl_mod._parser   = Parser()
    _nl_mod._executor = Executor(_engine)

    _opt_mod._engine   = _engine
    _opt_mod._parser   = Parser()
    _opt_mod._executor = Executor(_engine)

    _HAS_AGENTS = True
    print("[MiniDB] Agents loaded (DBA Monitor + Ask in English + Query Optimizer)")
except ImportError as e:
    print(f"[MiniDB] Agents unavailable ({e})")


# ── Agent command handlers ───────────────────────────────────────────────

def _handle_dba_monitor(msg: dict) -> dict:
    if not _HAS_AGENTS:
        return {"ok": False, "error": "Agent modules not available."}
    if not os.environ.get("GROQ_API_KEY"):
        return {"ok": False, "error": "GROQ_API_KEY not set."}
    auto_fix = msg.get("auto_fix", False)
    report = _dba_mod.run_monitor(auto_fix=auto_fix, verbose=False)
    return {"ok": True, "report": report}


def _handle_ask(msg: dict) -> dict:
    if not _HAS_AGENTS:
        return {"ok": False, "error": "Agent modules not available."}
    if not os.environ.get("GROQ_API_KEY"):
        return {"ok": False, "error": "GROQ_API_KEY not set."}
    question = (msg.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "Empty question"}
    result = _nl_mod.run_nl_to_sql(question, auto_run=True, verbose=False)
    if result.get("error"):
        return {"ok": False, "error": result["error"], "sql": result.get("sql")}
    resp = {"ok": True, "sql": result.get("sql")}
    if result.get("rows") is not None:
        resp["rows"] = result["rows"]
    if result.get("message"):
        resp["message"] = result["message"]
    if result.get("list"):
        resp["list"] = result["list"]
    return resp


def _handle_optimizer(msg: dict) -> dict:
    if not _HAS_AGENTS:
        return {"ok": False, "error": "Agent modules not available."}
    if not os.environ.get("GROQ_API_KEY"):
        return {"ok": False, "error": "GROQ_API_KEY not set."}
    sql = (msg.get("sql") or "").strip().rstrip(";").strip()
    if not sql:
        return {"ok": False, "error": "Empty query"}
    auto_fix = msg.get("auto_fix", False)
    report = _opt_mod.run_optimizer(sql, auto_fix=auto_fix, verbose=False)
    return {"ok": True, "report": report}


# ── Client handler ───────────────────────────────────────────────────────

def handle_client(conn: socket.socket, addr):
    peer = f"{addr[0]}:{addr[1]}"
    print(f"[MiniDB] Client connected: {peer}")

    parser   = Parser()
    executor = Executor(_engine)
    buf      = b""

    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    _send(conn, {"ok": False, "error": "Invalid JSON"})
                    continue

                # Route: agent command or SQL query
                cmd = msg.get("cmd")
                try:
                    if cmd == "dba-monitor":
                        _send(conn, _handle_dba_monitor(msg))
                    elif cmd == "ask":
                        _send(conn, _handle_ask(msg))
                    elif cmd == "query-optimizer":
                        _send(conn, _handle_optimizer(msg))
                    else:
                        # SQL query
                        sql = (msg.get("sql") or "").strip().rstrip(";").strip()
                        if not sql:
                            _send(conn, {"ok": False, "error": "Empty query"})
                            continue
                        node   = parser.parse(sql)
                        result = executor.execute(node)
                        clean = {k: v for k, v in result.items()
                                 if k not in ("plan", "exit") and v is not None}
                        _send(conn, {"ok": True, **clean})

                except (ParseError, KeyError, ValueError,
                        TypeError, RuntimeError) as e:
                    _send(conn, {"ok": False, "error": str(e)})
                except Exception as e:
                    _send(conn, {"ok": False, "error": f"Unexpected: {e}"})

    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        if _engine.txn_mgr.active:
            try:
                _engine.rollback()
                print(f"[MiniDB] Auto-rollback for {peer}")
            except Exception:
                pass
        conn.close()
        print(f"[MiniDB] Client disconnected: {peer}")


def _send(conn: socket.socket, data: dict):
    msg = json.dumps(data, default=str) + "\n"
    conn.sendall(msg.encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="MiniDB TCP Database Server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default=5433, type=int)
    args = ap.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(50)

    print(f"[MiniDB] TCP Server listening on {args.host}:{args.port}")

    try:
        while True:
            conn, addr = server.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr),
                                 daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[MiniDB] Server shutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
