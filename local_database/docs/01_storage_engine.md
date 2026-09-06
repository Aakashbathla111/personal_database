# Storage Engine (storage/)

Storage engine MiniDB ka **dil** hai — saari tables, WAL, buffer pool, transactions — sab iske andar hai. Har ek read/write iske through jaata hai.

## Files Involved
- `storage/engine.py` — Main boss (StorageEngine class)
- `storage/table.py` — Table ka structure — rows, columns, indexes
- `storage/column.py` — Column ki info — type, constraints
- `storage/wal.py` — Write-Ahead Log — crash recovery ke liye
- `storage/buffer_pool.py` — LRU cache — disk I/O kam karne ke liye
- `storage/page.py` — 4KB pages mein data store hota hai
- `storage/mvcc.py` — Multi-Version Concurrency Control
- `storage/index.py` — B+ tree ke upar wrapper

---

## StorageEngine (engine.py) — Main Boss

Ye poore database ka coordinator hai. Saari operations iske through jaati hain.

```
User Request → StorageEngine → Table → Disk/WAL
```

**Key operations:**
- **DDL**: `create_table()`, `drop_table()`, `truncate()` — table banao, hatao, khali karo
- **DML**: `insert()`, `select()`, `update()`, `delete()` — data daalo, nikalo, badlo, mitao
- **Index**: `create_index()`, `drop_index()` — fast lookup ke liye index banao
- **Recovery**: `recover()` — crash ke baad WAL se data wapas laao

**Har write operation mein ye pattern follow hota hai:**
1. Table ka lock lo (koi aur saath mein mess na kare)
2. **Pehle WAL mein likho** (agar crash ho jaaye toh recovery ho sake)
3. Memory mein change karo (MVCC stamps ke saath)
4. Disk pe save karo (JSON files + page files)
5. Lock chhod do

Socho jaise bank mein — pehle register mein entry, phir actually paisa transfer.

---

## Table (table.py) — Data Ka Ghar

Ek table mein hota hai:
- `columns` — column ki list (schema — kaunse columns hain, kya type hai)
- `rows` — list of dicts (actual data — har row ek dictionary hai)
- `indexes` — indexes ka dict (fast lookup ke liye)

**Key kaam:**
- `select()` — rows filter karta hai WHERE se, GROUP BY lagata hai, ORDER BY se sort karta hai
- `insert()` — naya row daalta hai, pehle type check karta hai, constraints check karta hai
- `update()` — internally ye purana row DELETE karke naya INSERT karta hai (MVCC style)
- `delete()` — row physically nahi hatata! Bas `_xmax` set kar deta hai (mark as dead)
- `join()` — INNER, LEFT, RIGHT, FULL, CROSS join support karta hai

**WHERE evaluation (`_eval_where`):**
Ye recursively conditions evaluate karta hai — AND, OR, NOT, BETWEEN, IN, LIKE, IS NULL, comparisons (=, !=, <, >, etc.)

Jaise: `WHERE age > 25 AND department = 'HR'`
→ Pehle `age > 25` check karo, phir `department = 'HR'` check karo, dono true hain toh row include karo.

---

## Column (column.py) — Column Ki Definition

Har column ki metadata yahan hai:
- **Valid types**: INT, TEXT, FLOAT, BOOL, DATE, BLOB (bas yehi 6 types hain)
- **Constraints**: PRIMARY KEY, NOT NULL, UNIQUE, DEFAULT, CHECK, FOREIGN KEY
- **`cast()` method**: Values ko sahi type mein convert karta hai (e.g., string "42" → integer 42)

---

## Write-Ahead Log — WAL (wal.py) — Crash Se Bachao

WAL ensure karta hai ki **crash hone pe bhi data na jaaye**.

**Kaise kaam karta hai:**
1. Koi bhi change karne se PEHLE, WAL file mein likho (`data/wal.log`)
2. Har record mein hota hai: LSN (sequence number), timestamp, txn_id, operation, data
3. `fsync` force karta hai ki record disk pe likh jaaye turant
4. Recovery ke time: committed operations replay karo, uncommitted skip karo

```
INSERT request aaya:
  → Pehle WAL.write({op: INSERT, table: employees, row: {...}})
  → fsync se disk pe likho (ab safe hai!)
  → Ab actually row insert karo memory mein
  → Table ko JSON file mein save karo
```

**Agar beech mein crash ho jaaye:**
- WAL file mein record hai → Recovery pe replay hoga → Data wapas aa jayega
- WAL mein record nahi hai → Wo change kabhi hua hi nahi → Koi problem nahi

**Recovery algorithm (REDO-only):**
1. WAL ke saare records padho
2. Dekho kaunsi transactions COMMIT hui thi
3. Sirf committed transactions replay karo
4. Uncommitted skip karo (wo incomplete thi, unhe ignore karo)

---

## Buffer Pool (buffer_pool.py) — Memory Cache

Ye ek **LRU (Least Recently Used) cache** hai jo disk aur memory ke beech baithta hai.

- Default capacity: 64 pages
- Jab koi page chahiye → pehle cache mein dekho
- Cache hit → great, disk pe jaane ki zaroorat nahi!
- Cache miss → disk se laao, cache mein daalo, purana (LRU) page hatao
- `mark_dirty()` — page modify hua hai, baad mein disk pe likhna padega
- `flush_all()` — saare dirty pages disk pe likh do

**Kyu important hai:** Bina cache ke, har query disk pe jaayegi — bahut slow. Cache se frequently accessed data memory mein rehta hai — fast!

Socho jaise ek library — baar baar padhne wali kitaabein table pe rakhte ho, almari se baar baar nahi nikalte.

---

## Page Storage (page.py) — Disk Pe Kaise Store Hota Hai

Tables **4KB pages** mein disk pe store hoti hain.

**Ek page ka layout:**
```
Bytes 0-3:   page_id (page number)
Bytes 4-7:   num_records (kitne records hain)
Bytes 8-11:  free_offset (agla record kahan likhna hai)
Bytes 12-15: reserved
Bytes 16+:   actual data (newline se separated JSON records)
```

`PageManager` `.tbl` files manage karta hai (har table ki ek file).

---

## MVCC (mvcc.py) — Multiple Transactions Ek Saath

**Multi-Version Concurrency Control** — ye allow karta hai ki multiple transactions ek saath kaam karein bina ek doosre ko block kiye. PostgreSQL bhi yehi use karta hai.

**Har row mein 2 hidden columns hote hain:**

| Column | Matlab |
|--------|--------|
| `_xmin` | Kaunsi transaction ne ye row **banaya** |
| `_xmax` | Kaunsi transaction ne ye row **delete kiya** (0 = abhi alive hai) |

**INSERT:**
```
Naya row → _xmin = meri txn_id, _xmax = 0 (alive hai)
```

**DELETE:**
```
Row physically nahi hatate!
Bas _xmax = meri txn_id set kar dete hain
Ab ye row future transactions ko dikhega nahi
```

**UPDATE:**
```
= DELETE purana + INSERT naya
Purana row → _xmax = meri txn_id
Naya row → _xmin = meri txn_id, _xmax = 0
```

**Row visible hai ya nahi? (Visibility Rule):**
```
Row dikhega Transaction T ko agar:
  1. _xmin committed hai (ya _xmin == T khud)
     AUR
  2. _xmax == 0 (kisi ne delete nahi kiya)
     YA _xmax committed nahi hai aur _xmax != T
```

**Example:**
```
Transaction 1: INSERT 'Alice' → row bani (_xmin=1, _xmax=0)
Transaction 1: COMMIT → ab sab ko dikhegi

Transaction 2: SELECT * → Alice dikhegi (T1 committed hai)

Transaction 3: DELETE 'Alice' → _xmax=3 set hua
Transaction 2 (abhi chal rahi hai): SELECT * → Alice ABHI BHI dikhegi!
  (kyunki T2 ne snapshot liya tha jab T3 start nahi hua tha)
```

**Vacuum:** Time ke saath dead rows pile up hoti hain. `VACUUM` command physically remove karta hai unhe jab koi active transaction unhe dekh nahi sakti.

---

## Index Wrapper (storage/index.py) — B+ Tree Ka Clean Interface

B+ tree ko ek simple API mein wrap karta hai:
- `build()` — saari existing rows scan karke index mein daalo
- `lookup(value)` — O(log n) exact match (ek value dhundho)
- `range_lookup(lo, hi)` — O(log n + k) range query (range mein dhundho)
- `insert(value, row_index)` — naya row aaye toh index update karo
- `delete(value, row_index)` — row delete ho toh index se bhi hatao

---

## Complete Flow — Ek INSERT Ka Poora Safar

```
SQL: INSERT INTO employees VALUES (51, 'New Guy', 28, 90000, 'Sales')

1. StorageEngine.insert("employees", [51, 'New Guy', 28, 90000, 'Sales'])
2. "employees" table ka lock lo
3. WAL.write({op: INSERT, table: employees, row: ...})  ← pehle log, crash safe!
4. Table.insert(row) — _xmin=current_txn, _xmax=0 set karo
5. Type validate karo (51=INT ok, 'New Guy'=TEXT ok, 28=INT ok, 90000=FLOAT ok)
6. Constraints check karo (PRIMARY KEY unique hai? NOT NULL toh nahi?)
7. Indexes update karo (B+ tree mein 'Sales' add karo)
8. data/tables/employees.json mein save karo
9. Lock release karo
```
