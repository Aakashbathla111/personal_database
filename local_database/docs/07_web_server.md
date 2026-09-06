# Web Server (web_server.py)

Web server **user-facing layer** hai — browser-based UI jo sab kuch ek jagah connect karta hai.

## File
- `web_server.py` — Flask app with embedded HTML/CSS/JS

---

## Architecture — Kaun Kya Karta Hai

```
Browser (http://127.0.0.1:8080)
  │
  ├── GET /               → HTML page serve karta hai (single-page app)
  │
  ├── POST /query         → SQL Query tab
  │     → Parser → Executor → StorageEngine → results
  │
  ├── POST /dba-monitor   → DBA Monitor tab
  │     → dba_monitor.run_monitor() → Groq API → health report
  │
  ├── POST /ask           → Ask in English tab
  │     → nl_to_sql.run_nl_to_sql() → Groq API → SQL + results
  │
  └── POST /query-optimizer → Query Optimizer tab
        → query_optimizer.run_optimizer() → Groq API → optimization report
```

---

## Startup Flow — Server Kaise Start Hota Hai

```python
# 1. Environment variables load karo (.env file se)
load_dotenv()  # GROQ_API_KEY padh leta hai

# 2. Shared database engine banao
_engine = StorageEngine()  # data/tables/*.json se tables load karta hai

# 3. Agent modules import karke wire karo
import agent.dba_monitor as _dba_mod
import agent.nl_to_sql as _nl_mod
import agent.query_optimizer as _opt_mod

# Agents ko same engine do — sab same data dekhein
_dba_mod._engine = _engine
_nl_mod._engine  = _engine
_opt_mod._engine = _engine

# 4. Flask server start karo
app.run(host="127.0.0.1", port=8080)
```

Socho jaise ek office — ek shared database (filing cabinet) hai, aur 4 alag departments (tabs) same cabinet se kaam karte hain.

---

## 4 Tabs — Har Ek Ka Kaam

### 1. SQL Query Tab (`POST /query`)

Direct SQL execution — raw SQL likho, results dekho:
```
User SQL likhta hai → POST /query {sql: "SELECT * FROM employees"}
  → Parser.parse(sql)
  → Executor.execute(ast_node)
  → Return JSON: {ok: true, rows: [...], count: 50}
  → Browser mein table render hota hai
```

Features:
- SQL editor textarea
- Ctrl+Enter se run karo
- Query history (click karke dubara run karo)
- Error display galat SQL ke liye

### 2. DBA Monitor Tab (`POST /dba-monitor`)

```
User "Run Health Check" click karta hai
  → POST /dba-monitor {auto_fix: false}
  → dba_monitor.run_monitor(auto_fix=False)
  → LLM tools call karta hai: get_table_stats → check_wal_health → list_indexes
  → LLM health report generate karta hai
  → Return JSON: {ok: true, report: "SUMMARY: ..."}
  → Browser mein report dikhta hai
```

### 3. Ask in English Tab (`POST /ask`)

```
User likhta hai: "show me employees earning more than 100k"
  → POST /ask {question: "show me employees earning more than 100k"}
  → nl_to_sql.run_nl_to_sql(question)
  → LLM generate karta hai: "SELECT * FROM employees WHERE salary > 100000"
  → Agar SELECT hai: auto-execute karke rows return karo
  → Agar INSERT/CREATE hai: sirf SQL dikhao, execute mat karo
  → Return JSON: {ok: true, sql: "SELECT ...", rows: [...]}
  → Browser mein generated SQL + result table dikhta hai
```

### 4. Query Optimizer Tab (`POST /query-optimizer`)

```
User paste karta hai: "SELECT * FROM orders WHERE status = 'pending'"
  → POST /query-optimizer {sql: "...", auto_fix: false}
  → query_optimizer.run_optimizer(sql)
  → LLM tools call karta hai: explain → stats → indexes → benchmark
  → LLM optimization report generate karta hai
  → Return JSON: {ok: true, report: "ORIGINAL QUERY: ..."}
  → Browser mein report dikhta hai
```

---

## Thread Safety — Concurrent Requests

Web server multiple requests ek saath handle karta hai:
- `_engine_lock` — ek threading.Lock jo engine access serialize karta hai
- Per-table RLocks StorageEngine ke andar
- Flask ka `threaded=True` mode

```python
with _engine_lock:
    node = parser.parse(sql)
    result = executor.execute(node)
```

Ye ensure karta hai ki do users ek saath query run karein toh data corrupt na ho.

---

## Frontend — Browser Mein Kya Dikhta Hai

Poora UI ek HTML string hai jo `web_server.py` mein embedded hai:

- **Dark theme** — GitHub-inspired dark mode (aankhen na thakein)
- **Tab switching** — JavaScript se tab panels show/hide hote hain
- **Async fetch** — saare API calls `fetch()` se JSON mein hote hain
- **Result rendering** — query results dynamically table mein render hote hain
- **Spinner** — loading animation jab agents soch rahe hote hain
- **Error handling** — red error messages failures ke liye

Ye pure vanilla HTML/CSS/JS hai — koi React nahi, koi npm nahi, koi build tools nahi. Bas plain code. Ek file mein poora frontend.
