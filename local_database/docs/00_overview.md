# MiniDB — Poora System Kaise Kaam Karta Hai

MiniDB ek **relational database hai jo Python mein scratch se banaaya gaya hai** with AI-powered features. Ye folder har component ko detail mein explain karta hai.

---

## Big Picture — Sabka Connection

```
                        ┌──────────────────┐
                        │   Web Browser     │
                        │  (localhost:8080)  │
                        └────────┬─────────┘
                                 │ HTTP
                        ┌────────▼─────────┐
                        │   Web Server      │
                        │  (Flask app)      │
                        │                   │
                        │  4 Tabs:          │
                        │  - SQL Query      │
                        │  - DBA Monitor    │
                        │  - Ask in English │
                        │  - Query Optimizer│
                        └────────┬─────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                   │
     ┌────────▼───────┐  ┌──────▼──────┐  ┌────────▼────────┐
     │  AI Agents      │  │   Parser    │  │   Executor      │
     │  (Groq LLM)     │  │  SQL → AST  │  │  AST → Results  │
     │                  │  └──────┬──────┘  │  + Query Planner│
     │ - DBA Monitor    │         │         └────────┬────────┘
     │ - NL to SQL      │         │                  │
     │ - Query Optimizer│         └──────────────────┤
     └─────────────────┘                             │
                                            ┌────────▼────────┐
                                            │  Storage Engine  │
                                            │                  │
                                            │  - Tables        │
                                            │  - MVCC          │
                                            │  - WAL           │
                                            │  - Buffer Pool   │
                                            │  - B+ Tree Index │
                                            │  - Transactions  │
                                            └────────┬────────┘
                                                     │
                                            ┌────────▼────────┐
                                            │    Disk          │
                                            │  data/tables/    │
                                            │  data/wal.log    │
                                            │  data/pages/     │
                                            └─────────────────┘
```

---

## Query Lifecycle: Start se End Tak

Jab tum SQL tab mein type karte ho `SELECT * FROM employees WHERE salary > 100000`:

```
Step 1: LEXER (parser/lexer.py)
  SQL string ko todta hai tokens mein — jaise sentence ke words
  "SELECT * FROM employees WHERE salary > 100000"
  → ["SELECT", "*", "FROM", "employees", "WHERE", "salary", ">", "100000"]

Step 2: PARSER (parser/parser.py)
  Tokens ko samajhta hai aur ek tree (AST) banata hai
  → SelectNode(columns=["*"], table="employees",
               where=["salary", ">", 100000])

Step 3: QUERY PLANNER (executor/planner.py)
  Decide karta hai ki query KAISE execute hogi
  Index hai salary pe? → Nahi → FullScan (saari rows check karo)
  Index hai? → Haan → RangeScan (B+ tree se seedha jaao)

Step 4: EXECUTOR (executor/executor.py)
  Engine ko bolta hai "ye kaam karo"
  → engine.select("employees", where=...)

Step 5: STORAGE ENGINE (storage/engine.py)
  → Table lock karo (doosra koi same time pe mess na kare)
  → Rows filter karo WHERE condition se
  → MVCC check karo (sirf committed data dikhao)
  → ORDER BY, LIMIT lagao
  → Lock release karo

Step 6: RESPONSE
  → {"rows": [{"id": 8, "name": "Hank", "salary": 129037.35, ...}, ...]}
  → Browser mein table render hota hai
```

---

## Documentation Files

| File | Component | Kya Sikhoge |
|------|-----------|-------------|
| [01_storage_engine.md](01_storage_engine.md) | Storage Layer | Tables, WAL, Buffer Pool, MVCC, Pages |
| [02_parser.md](02_parser.md) | SQL Parser | Lexer, AST, Parsing kaise hoti hai |
| [03_executor.md](03_executor.md) | Query Execution | Executor, Query Planner, Plan Types |
| [04_transactions.md](04_transactions.md) | Transactions | ACID, MVCC, Snapshots, Savepoints |
| [05_indexes.md](05_indexes.md) | B+ Tree Indexes | Tree Structure, Lookups, Range Scans |
| [06_agents.md](06_agents.md) | AI Agents | DBA Monitor, NL-to-SQL, Query Optimizer |
| [07_web_server.md](07_web_server.md) | Web UI | Flask, Tabs, API Endpoints, Frontend |

---

## Key Design Decisions — Kya Kyu Choose Kiya

| Decision | Kyu |
|----------|-----|
| **MVCC over row locking** | Readers ko writers block nahi karte, dono saath chale |
| **B+ Trees indexes ke liye** | Exact match AUR range queries dono fast hoti hain |
| **WAL (Write-Ahead Log)** | Crash ho jaaye toh bhi committed data kabhi nahi jayega |
| **Per-table locks** | Alag alag tables pe ek saath kaam ho sakta hai |
| **Page-based storage** | Real databases (PostgreSQL, MySQL) bhi aise hi karte hain |
| **LLM agents with tool use** | AI khud database inspect karke optimize kar sakta hai |
| **Single shared engine** | Web server aur agents sab same live data dekhte hain |
