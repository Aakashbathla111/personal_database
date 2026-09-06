# AI Agents (agent/)

MiniDB mein teen AI-powered agents hain jo Groq ke LLM (qwen3.8-27b) use karte hain intelligent database features dene ke liye.

## Files Involved
- `agent/dba_monitor.py` — Database health checker (autonomous)
- `agent/nl_to_sql.py` — English mein pucho, SQL mein jawaab
- `agent/query_optimizer.py` — Query analyze karke optimize kare

Saare agents **Groq API** (free) use karte hain `qwen/qwen3.8-27b` model ke saath.

---

## 1. DBA Monitor (dba_monitor.py) — Database Ka Doctor

### Kya Karta Hai
Ek autonomous agent jo tumhara database inspect karta hai, problems dhundhta hai, aur fixes suggest karta hai — jaise ek DBA (Database Admin) available ho 24/7.

### Kaise Kaam Karta Hai (Tool-Use Pattern)

Ye ek **agentic loop** use karta hai — LLM khud decide karta hai kaunsa tool call karna hai, results interpret karta hai, aur phir aur tools call karta hai jab tak poora analysis na ho jaaye.

```
┌─────────────────────────────────────────┐
│  1. LLM ko bhejo: system prompt + tools │
│  2. LLM decide karta hai: "pehle       │
│     get_table_stats call karta hoon"    │
│  3. Hum get_table_stats() execute karte │
│     hain aur results wapas bhejte hain  │
│  4. LLM: "ab check_wal_health dekhta   │
│     hoon"                               │
│  5. Hum check_wal_health() execute karte│
│  6. LLM: "ab list_indexes dekhta hoon" │
│  7. Hum list_indexes() execute karte    │
│  8. LLM: "enough data hai mere paas"   │
│  9. LLM final health report generate   │
│     karta hai                           │
└─────────────────────────────────────────┘
```

Socho jaise ek doctor — pehle blood test karega, phir X-ray, phir kuch aur tests, phir sab results dekh ke diagnosis dega. Doctor (LLM) khud decide karta hai kaunsa test karna hai.

### Available Tools

| Tool | Kya Karta Hai |
|------|--------------|
| `get_table_stats()` | Saari tables ka info — rows, columns, types |
| `check_wal_health()` | WAL file ki health — size, uncommitted records |
| `list_indexes()` | Kaunse tables pe indexes hain kaunse pe nahi |
| `run_query()` | Read-only SELECT/SHOW/DESCRIBE execute karo |
| `create_index()` | Missing index banao (sirf auto-fix ON ho toh) |

### Kya Issues Dhundhta Hai
- Bahut rows wali table pe koi index nahi — slow queries!
- WAL bloat — bahut saare uncommitted records
- Missing indexes — WHERE mein use hone wale columns pe index nahi
- Recovery needed — WAL mein uncommitted data pada hai

### Output Kaise Dikhta Hai
```
SUMMARY   — ek paragraph mein overall assessment
ISSUES    — problems ki list (severity: LOW/MEDIUM/HIGH)
ACTIONS   — kya kiya ya kya karna chahiye
METRICS   — numbers (table sizes, WAL records, index coverage)
```

---

## 2. Ask in English — NL to SQL (nl_to_sql.py) — English Mein Pucho

### Kya Karta Hai
Tum plain English mein question type karo, AI SQL query generate karta hai, aur read-only queries ke liye automatically execute bhi karta hai.

### Kaise Kaam Karta Hai (Single-Shot Pattern)

DBA Monitor se alag, ye ek **single LLM call** hai — koi loop nahi:

```
┌─────────────────────────────────────────┐
│  1. Database schema gather karo         │
│     - Saari tables, columns, types      │
│     - Sample data (pehli 3 rows)        │
│                                         │
│  2. LLM ko bhejo:                       │
│     System: "Tu SQL generator hai"      │
│     User: schema + question             │
│                                         │
│  3. LLM raw SQL string return karta hai │
│                                         │
│  4. Safety check:                       │
│     - SELECT/SHOW/DESCRIBE → auto-run   │
│     - INSERT/UPDATE/DELETE → sirf dikhao│
│                                         │
│  5. Agar auto-run: parse + execute SQL  │
│  6. SQL + results user ko dikhao        │
└─────────────────────────────────────────┘
```

### Schema Injection — Ye Key Trick Hai

LLM ko database ka poora schema bhejte hain question ke saath. Isse LLM ko pata chalta hai kaunsi tables hain, kaunse columns hain, kya types hain — toh wo sahi SQL generate kar paata hai.

```
Table: employees
  - id (INT, PRIMARY KEY)
  - name (TEXT, NOT NULL)
  - age (INT)
  - salary (FLOAT)
  - department (TEXT)
  Sample data (50 total rows):
    {'id': 1, 'name': 'Alice', 'age': 29, 'salary': 42751.18, ...}
```

### Safety Rules
- **Read queries** (SELECT, SHOW, DESCRIBE): SQL generate AURU execute — results dikhao
- **Write queries** (CREATE, INSERT, UPDATE, DELETE): SQL generate karo par EXECUTE MAT KARO — user ko bolo SQL tab mein copy karo
- Vague questions (bina table/column name ke) reject ho jaati hain

---

## 3. Query Optimizer (query_optimizer.py) — Query Ko Fast Banao

### Kya Karta Hai
SQL query leta hai, uska execution plan analyze karta hai, benchmark karta hai, bottlenecks dhundhta hai, aur optimization suggest karta hai.

### Kaise Kaam Karta Hai (Agentic Loop Pattern)

DBA Monitor jaisa — LLM multi-step analysis drive karta hai:

```
┌─────────────────────────────────────────┐
│  Step 1: explain_query(original SQL)    │
│    → Current plan dekho (FullScan hai   │
│      ya Index scan?)                    │
│                                         │
│  Step 2: get_table_stats()              │
│    → Table kitni badi hai?              │
│                                         │
│  Step 3: list_indexes()                 │
│    → Kaunse indexes hain?              │
│                                         │
│  Step 4: benchmark_query(original SQL)  │
│    → Kitna time lag raha hai? (5 baar   │
│      run karke average nikalo)          │
│                                         │
│  Step 5 (auto-fix on): create_index()   │
│    → Missing indexes banao              │
│                                         │
│  Step 6: explain_query(optimized SQL)   │
│    → Plan improve hua?                  │
│                                         │
│  Step 7: benchmark_query(optimized SQL) │
│    → Ab kitna time lag raha hai?        │
│                                         │
│  Step 8: Comparison report banao        │
│    → Before vs After                    │
└─────────────────────────────────────────┘
```

Socho jaise ek car mechanic — pehle car test drive karta hai (benchmark), phir engine check karta hai (explain), phir tune-up karta hai (create_index), phir dobara test drive (benchmark again) aur bolta hai "pehle itna time lagta tha, ab itna lagta hai".

### Available Tools

| Tool | Kya Karta Hai |
|------|--------------|
| `explain_query(sql)` | Execution plan dikhao bina run kiye |
| `benchmark_query(sql)` | 5 baar run karo, avg/min/max ms nikalo |
| `get_table_stats()` | Table sizes, column info |
| `list_indexes()` | Existing indexes |
| `create_index()` | Index banao (sirf auto-fix ON ho toh) |

### Kya Issues Dhundhta Hai
- FullScan badi table pe jahan filter ho raha hai — index chahiye!
- Missing indexes WHERE / JOIN columns pe
- `SELECT *` jab sirf kuch columns chahiye
- Non-sargable predicates (jaise `LIKE '%x'` index use nahi kar sakta)

### Output Kaise Dikhta Hai
```
ORIGINAL QUERY    — jo query di thi
ORIGINAL PLAN     — FullScan / IndexScan / RangeScan
OPTIMISED QUERY   — rewritten SQL (ya "no rewrite needed")
OPTIMISED PLAN    — changes ke baad kya plan hai
INDEXES CREATED   — kaunse indexes banaye (ya "none")
TIMING            — before vs after (ms), speedup factor
RECOMMENDATIONS   — kya kiya aur kyu
```

---

## Agents Web Server Se Kaise Connect Hain

Teeno agents **same StorageEngine instance** share karte hain web server ke saath:

```python
# web_server.py mein:
_engine = StorageEngine()  # ek shared engine

# Agents ko same engine do:
_dba_mod._engine = _engine
_nl_mod._engine  = _engine
_opt_mod._engine = _engine
```

Iska matlab:
- SQL tab mein table banao → DBA Monitor turant dekh sakta hai
- Optimizer index banaye (auto-fix) → SQL queries turant fast ho jaati hain
- Sab sync mein rehta hai kyunki same in-memory database hai

---

## Groq API Ka Pattern

Saare agents ye pattern use karte hain:

```python
from groq import Groq

client = Groq()  # GROQ_API_KEY environment se padh leta hai

# Simple call (nl_to_sql):
response = client.chat.completions.create(
    model="qwen/qwen3.8-27b",
    messages=[
        {"role": "system", "content": "Tu SQL generator hai..."},
        {"role": "user", "content": "show me all employees..."},
    ]
)
sql = response.choices[0].message.content

# Tool-use call (dba_monitor, query_optimizer):
response = client.chat.completions.create(
    model="qwen/qwen3.8-27b",
    tools=[...tool definitions...],
    messages=[...conversation history...]
)
# Agar finish_reason == "tool_calls":
#   Tools execute karo, results wapas bhejo, loop karo
# Agar finish_reason == "stop":
#   Done! Final text return karo
```
