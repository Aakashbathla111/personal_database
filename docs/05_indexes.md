# B+ Tree Indexes (index/)

Indexes queries ko fast banate hain. Bina index ke, har query SAARI rows scan karti hai (O(n)). Index se lookups O(log n) mein hoti hain — bahut faster.

## Files Involved
- `index/bplustree.py` — B+ tree data structure
- `storage/index.py` — Index wrapper jo B+ tree ko tables se connect karta hai

---

## B+ Tree Kya Hai?

Ek self-balancing tree jo databases ke liye optimized hai. Socho jaise ek sorted phone directory — naam alphabetically arranged hai, toh directly page pe jaake naam dhundh sakte ho, sab pages ek ek karke nahi padhne padte.

**Structure:**
- **Internal nodes** — sirf routing ke liye (keys + pointers to children)
- **Leaf nodes** — actual data yahan hai (keys + row indices)
- **Leaf nodes linked hain** — range scans ke liye ek se doosre pe walk kar sakte ho
- MiniDB mein order=4 (max 3 keys per leaf, max 4 children per internal node)

```
         [30, 60]              ← Internal node (guide karta hai)
        /    |    \
   [10,20] [30,40,50] [60,70] ← Leaf nodes (actual data)
      →         →        →    ← next pointers (linked list)
```

---

## Lookups Kaise Kaam Karti Hain

### Exact Match: `WHERE department = 'Engineering'`

```
1. Root node se start karo
2. 'Engineering' ko keys se compare karo, sahi child pe jaao
3. Leaf node tak pahuncho
4. 'Engineering' dhundho → [row indices: 2, 5, 11, 16, 18, ...]
5. Sirf un specific rows ko table se fetch karo
```
**Time: O(log n)** — 50 rows scan karne ki jagah, bas 3 tree levels traverse karo.

Jaise library mein catalogue check karke seedha shelf pe jaao — saari shelves check nahi karni.

### Range Query: `WHERE salary > 100000`

```
1. Root se navigate karke 100000 wali leaf pe jaao
2. Leaf linked list pe walk karo (→ next → next)
3. Saare row indices collect karo jahan key > 100000
4. Jab range khatam ho ya end aaye, ruk jaao
```
**Time: O(log n + k)** jahan k = matching rows ki count.

Jaise phone directory mein 'S' se shuru karne wale names dhundhne hain — 'S' page pe jaao, phir aage padhte jaao.

---

## Index Maintenance — Insert/Delete Pe Kya Hota Hai

### INSERT Pe
```
INSERT INTO employees VALUES (51, 'New Guy', 28, 90000, 'Sales')
  → Har index ke liye:
    → index.insert(value='Sales', row_index=50)
    → B+ tree mein entry add hoti hai, node full ho toh split hota hai
```

### DELETE Pe
```
DELETE FROM employees WHERE id = 51
  → Har index ke liye:
    → index.delete(value='Sales', row_index=50)
    → B+ tree se entry hatti hai
```

### UPDATE Pe (indexed column change ho)
```
UPDATE employees SET department = 'HR' WHERE id = 5
  → Purani entry hatao: index.delete('Engineering', 4)
  → Nayi entry daalo: index.insert('HR', 50)
  (kyunki UPDATE = DELETE purana + INSERT naya MVCC mein)
```

---

## Index Banana

```sql
-- Basic index
CREATE INDEX idx_orders_status ON orders(status)

-- Unique index (uniqueness enforce karta hai)
CREATE UNIQUE INDEX idx_employees_id ON employees(id)
```

**Internally kya hota hai:**
1. Naya Index object banta hai jismein fresh B+ tree hai
2. `build()` — SAARI existing rows scan karo, har ek ko tree mein daalo
3. Table pe index register karo
4. Ab se query planner IndexScanPlan choose kar sakta hai!

---

## Performance Ka Farak

```
Table: orders (100 rows)

INDEX NAHI hai "status" pe:
  SELECT * FROM orders WHERE status = 'pending'
  → FullScanPlan: saari 100 rows check karo → ~25 matches
  → Time: O(100)

INDEX HAI "status" pe:
  SELECT * FROM orders WHERE status = 'pending'
  → IndexScanPlan: B+ tree lookup → seedha 25 row indices mil gaye
  → Time: O(log 100 + 25) ≈ O(32)

  ~3x faster, aur table badi hogi toh gap aur zyada badhega!
```

```
1,000 rows:    FullScan = O(1000)   vs  IndexScan = O(35)     → 28x faster
10,000 rows:   FullScan = O(10000)  vs  IndexScan = O(40)     → 250x faster
1,000,000 rows: FullScan = O(1M)    vs  IndexScan = O(50ish)  → 20,000x faster!
```

---

## Key Features

- **Duplicate keys**: Ek key pe multiple rows ho sakti hain (jaise bahut saare employees 'Engineering' mein)
- **Open-ended ranges**: `WHERE salary > 100000` (upper bound nahi hai, koi baat nahi)
- **Rebuild after delete**: Jab vacuum physically rows hatata hai, indexes apne row indices shift karte hain
- **Snapshot/restore**: Transaction rollback ke liye indexes apna state save aur restore kar sakte hain
