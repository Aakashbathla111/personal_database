"""
MiniDB TCP Client  —  db_client.py

Python client library to connect to MiniDB TCP server.

Usage:
    from db_client import MiniDBClient

    db = MiniDBClient("127.0.0.1", 5433)
    db.connect()

    rows = db.query("SELECT * FROM employees WHERE age > 30")
    print(rows)

    db.execute("INSERT INTO test VALUES (1, 'hello')")

    db.execute("BEGIN")
    db.execute("INSERT INTO test VALUES (2, 'world')")
    db.execute("COMMIT")

    db.close()
"""

import json
import socket


class MiniDBClient:

    def __init__(self, host="127.0.0.1", port=5433):
        self.host = host
        self.port = port
        self._sock = None
        self._buf  = b""

    def connect(self):
        """Connect to MiniDB TCP server."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.host, self.port))
        return self

    def close(self):
        """Close the connection."""
        if self._sock:
            self._sock.close()
            self._sock = None

    def _send_recv(self, sql: str) -> dict:
        """Send SQL, receive JSON response."""
        if not self._sock:
            raise ConnectionError("Not connected. Call connect() first.")

        msg = json.dumps({"sql": sql}) + "\n"
        self._sock.sendall(msg.encode("utf-8"))

        # Read until we get a complete line
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("Server closed connection")
            self._buf += chunk

        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def query(self, sql: str) -> list[dict]:
        """Execute a SELECT query, return list of row dicts."""
        result = self._send_recv(sql)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Unknown error"))
        return result.get("rows", [])

    def execute(self, sql: str) -> str:
        """Execute a non-SELECT statement, return message."""
        result = self._send_recv(sql)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Unknown error"))
        return result.get("message", "OK")

    def raw(self, sql: str) -> dict:
        """Execute any SQL, return full response dict."""
        return self._send_recv(sql)

    def send_cmd(self, cmd: dict) -> dict:
        """Send a raw command dict to the server (for agent commands)."""
        if not self._sock:
            raise ConnectionError("Not connected. Call connect() first.")
        msg = json.dumps(cmd) + "\n"
        self._sock.sendall(msg.encode("utf-8"))
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("Server closed connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    # Context manager support
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


# ── Interactive CLI mode ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5433

    print(f"Connecting to MiniDB at {host}:{port}...")

    try:
        with MiniDBClient(host, port) as db:
            print("Connected! Type SQL queries (Ctrl-C to quit)\n")
            while True:
                try:
                    sql = input("minidb> ").strip()
                    if not sql:
                        continue
                    if sql.lower() in ("exit", "quit"):
                        break

                    result = db.raw(sql)
                    if not result.get("ok"):
                        print(f"ERROR: {result.get('error')}")
                    elif result.get("rows"):
                        for row in result["rows"]:
                            print(row)
                        print(f"({len(result['rows'])} rows)")
                    elif result.get("list"):
                        for item in result["list"]:
                            print(f"  - {item}")
                    elif result.get("message"):
                        print(result["message"])
                except EOFError:
                    break
    except ConnectionRefusedError:
        print(f"Could not connect to {host}:{port}. Is db_server.py running?")
    except KeyboardInterrupt:
        print("\nBye!")
