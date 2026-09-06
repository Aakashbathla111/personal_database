"""
Lexer — converts raw SQL text into a flat list of tokens.

Rules:
  • SQL keywords, identifiers, operators → individual tokens
  • Single-quoted strings → kept as 'value' (quotes preserved)
  • Numbers → kept as-is
  • Parentheses, commas, semicolons, operators → individual tokens
  • Whitespace is ignored
  • Comments (-- ...) are stripped

Example
-------
Input : SELECT id, name FROM users WHERE age > 20 LIMIT 5;
Output: ['SELECT', 'id', ',', 'name', 'FROM', 'users',
         'WHERE', 'age', '>', '20', 'LIMIT', '5']
"""

import re


# Two-character operators must be matched before single-char ones
_TOKEN_RE = re.compile(
    r"'[^']*'"             # single-quoted string
    r"|--[^\n]*"           # single-line comment  (captured so we can discard)
    r"|!=|<>|<=|>="        # two-char operators
    r"|[=<>!]"             # single-char operators
    r"|[(),;*]"            # punctuation
    r"|\b\d+(?:\.\d+)?\b"  # numbers
    r"|[A-Za-z_]\w*"       # identifiers / keywords
    r"|[^\s]"              # any other non-space char (catch-all)
)


class Lexer:

    def tokenize(self, sql: str) -> list[str]:
        """
        Tokenize *sql* and return a list of string tokens.
        Comments and semicolons are stripped from the output.
        """
        sql = sql.strip()
        tokens = []
        for m in _TOKEN_RE.finditer(sql):
            tok = m.group(0)
            if tok.startswith("--"):   # comment → discard
                continue
            if tok == ";":             # statement terminator → discard
                continue
            tokens.append(tok)
        return tokens


# ── Quick self-test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    samples = [
        "SELECT * FROM users",
        "SELECT id, name FROM users WHERE age > 20 LIMIT 5",
        "INSERT INTO users (id, name) VALUES (1, 'Alice')",
        "UPDATE users SET name = 'Bob' WHERE id = 1",
        "DELETE FROM users WHERE id = 1",
        "CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)",
        "BEGIN; INSERT INTO t VALUES (1,'x'); COMMIT;",
        "SELECT COUNT(*) FROM users GROUP BY dept HAVING COUNT(*) > 1",
    ]
    lex = Lexer()
    for s in samples:
        print(f"SQL   : {s}")
        print(f"Tokens: {lex.tokenize(s)}")
        print()
