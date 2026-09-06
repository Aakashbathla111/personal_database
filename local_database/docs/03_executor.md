# Executor & Query Planner (executor/)

Executor wo **bridge** hai jo parsed SQL (AST) ko lekar storage engine se actual kaam karwata hai. Ye decide bhi karta hai ki query KAISE execute hogi (Planner).

## Files Involved
- `executor/executor.py` — Main query dispatcher
- `executor/planner.py` — Decide karta hai full scan karna hai ya index use karna hai

---

## Executor (executor.py) — Kaam Karwane Wala

Executor ko AST node milta hai aur wo sahi handler ko call karta hai.

**SQL ka poora pipeline:**
```
SQL string
  → Lexer.tokenize()       → tokens
  → Parser.parse()          → AST node
  → QueryPlanner.plan()     → execution plan (SELECT ke liye)
  → Executor.execute()      → result dict
  → StorageEngine methods   → actual data operations
```

**`execute(node)` method** ek bada dispatcher hai — node ka type dekho aur sahi kaam karo:
```python
def execute(node):
    if isinstance(node, SelectNode):      → SELECT handle karo
    if isinstance(node, InsertNode):      → INSERT handle karo
    if isinstance(node, UpdateNode):      → UPDATE handle karo
    if isinstance(node, DeleteNode):      → DELETE handle karo
    if isinstance(node, CreateTableNode): → CREATE TABLE handle karo
    if isinstance(node, BeginNode):       → BEGIN transaction
    # ... aur 25+ node types ke liye
```

### SELECT Execution — Sabse Complex

```
1. Subqueries resolve karo
   jaise: "id IN (SELECT emp_id FROM orders)"
   → Pehle andar wala SELECT execute karo
   → Replace karo: "id IN (1, 5, 12, ...)"

2. QueryPlanner se pucho — scan strategy kya hogi?
   → FullScanPlan, IndexScanPlan, ya RangeScanPlan

3. engine.select() ya engine.join() call karo
   → WHERE, GROUP BY, HAVING, ORDER BY, LIMIT pass karo

4. Agar EXPLAIN maanga tha toh plan details return karo, rows nahi

5. Result dict return karo:
   {"rows": [...], "count": 10, "plan": plan_object}
```

### INSERT Execution

```
1. Check karo ki column count aur value count match kare
2. Multi-row support: INSERT INTO t VALUES (1,'a'), (2,'b'), (3,'c')
3. Har row ke liye engine.insert(table, values) call karo
4. Return: {"message": "3 row(s) inserted."}
```

### UPDATE Execution

```
1. SET assignments parse karo: SET salary = 80000, department = 'HR'
2. engine.update(table, assignments, where) call karo
   → Engine matching rows dhundhti hai, MVCC update karti hai
3. Return: {"message": "5 row(s) updated."}
```

### DELETE Execution

```
1. engine.delete(table, where) call karo
   → Engine matching rows pe _xmax set karti hai (MVCC soft delete)
2. Return: {"message": "3 row(s) deleted."}
```

### Transaction Control

```
BEGIN    → engine.txn_manager.begin()    → naya transaction start, txn_id milta hai
COMMIT   → engine.txn_manager.commit()   → txn committed mark, disk pe save
ROLLBACK → engine.txn_manager.rollback() → saare changes undo karo

SAVEPOINT sp1          → ek checkpoint banao
ROLLBACK TO sp1        → checkpoint tak wapas jaao
RELEASE SAVEPOINT sp1  → checkpoint hata do
```

---

## Query Planner (planner.py) — Strategy Decide Karne Wala

Planner decide karta hai ki SELECT query **kaise** execute hogi — BEFORE actually execute karne se. Jaise GPS pehle route decide karta hai, phir chalna shuru karte ho.

### Teen Plan Types

**1. FullScanPlan (O(n)) — Sabse Slow**
- Table ki SAARI rows scan karta hai, ek ek karke
- Kab use hota hai: koi usable index nahi, complex WHERE, JOINs, OR conditions
- Hamesha kaam karta hai par slow hai

**2. IndexScanPlan (O(log n)) — Fast**
- B+ tree index use karta hai exact match ke liye
- Kab use hota hai: WHERE mein `column = value` hai AUR column pe index hai
- Point queries ke liye bahut fast

**3. RangeScanPlan (O(log n + k)) — Range Ke Liye Fast**
- B+ tree range search use karta hai
- Kab use hota hai: WHERE mein `column > value`, `column BETWEEN x AND y`
- Range queries ke liye fast

### Planner Kaise Decide Karta Hai

```
WHERE clause hai?
  ├─ Nahi → FullScanPlan (koi filter nahi, sab scan karo)
  └─ Haan
      ├─ Simple equality (col = val) AUR col pe index hai?
      │   └─ Haan → IndexScanPlan (B+ tree se seedha jaao)
      ├─ Range condition (col > val, col BETWEEN x AND y) AUR col pe index hai?
      │   └─ Haan → RangeScanPlan (B+ tree range scan)
      └─ Baaki sab (OR, multiple columns, no index)
          └─ FullScanPlan (no choice, scan karo)
```

### EXPLAIN Output

Har plan ek explain dict de sakta hai:
```python
{
    "plan_type": "IndexScanPlan",
    "table": "employees",
    "index": "idx_employees_department",
    "column": "department",
    "filter": "department = 'Engineering'",
    "estimated_rows": 12
}
```

### Example — Farak Samjho

```sql
-- "status" pe index NAHI hai → FullScanPlan (100 rows scan)
SELECT * FROM orders WHERE status = 'pending'
-- Sab rows check karni padegi. Slow.

-- "department" pe index HAI → IndexScanPlan (B+ tree lookup)
SELECT * FROM employees WHERE department = 'Engineering'
-- Seedha B+ tree se 12 rows mil gayi. Fast!

-- "salary" pe index banao, phir:
-- SELECT * FROM employees WHERE salary > 100000 → RangeScanPlan
-- B+ tree mein 100000 se start karke aage walk karo. Fast!
```

---

## Executor + Planner Saath Kaise Kaam Karte Hain

```
SQL: SELECT name FROM employees WHERE department = 'Engineering'

1. Parser banata hai:
   SelectNode(columns=["name"], table="employees",
              where=["department", "=", "Engineering"])

2. Executor Planner ko puchta hai:
   plan = planner.plan(select_node, engine)
   → Planner check karta hai: "employees" pe "department" ka index hai?
   → Haan! → IndexScanPlan return karta hai

3. Executor Engine ko bolta hai:
   engine.select("employees", columns=["name"],
                 where=["department", "=", "Engineering"])

4. Engine internally index use karti hai:
   index.lookup("Engineering") → [row indices: 2, 5, 11, ...]
   → Sirf un rows ko fetch karo, 50 mein se sab scan nahi karna

5. Return: {"rows": [{"name": "Bob"}, {"name": "Eve"}, ...], "count": 12}
```

Socho jaise ek library mein kitaab dhundhni hai — FullScan matlab ek ek shelf check karo, IndexScan matlab catalogue mein dekho aur seedha shelf pe jaao.
