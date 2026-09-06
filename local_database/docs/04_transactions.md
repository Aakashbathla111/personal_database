# Transactions & MVCC (transaction/ + storage/mvcc.py)

MiniDB mein full **ACID transactions** hain MVCC (Multi-Version Concurrency Control) ke saath — bilkul jaise PostgreSQL karta hai.

## Files Involved
- `transaction/manager.py` — Transaction lifecycle management
- `storage/mvcc.py` — MVCC visibility rules aur snapshots

---

## Transactions Kya Hain?

Transaction multiple operations ko ek group mein bandh deta hai:
- Ya toh SAARI operations succeed hongi (COMMIT)
- Ya KUCH BHI nahi hoga (ROLLBACK)

```sql
BEGIN;
UPDATE accounts SET balance = balance - 500 WHERE id = 1;
UPDATE accounts SET balance = balance + 500 WHERE id = 2;
COMMIT;
-- Dono updates ek saath honge, ya dono cancel
-- Aisa nahi hoga ki paisa katay par transfer na ho
```

Socho jaise bank transfer — ya toh dono entries hon, ya koi bhi nahi.

---

## Transaction Manager (transaction/manager.py)

### Transaction Ka Lifecycle

```
BEGIN
  → Unique txn_id milti hai (1, 2, 3... badhti jaati hai)
  → MVCC snapshot liya jaata hai (abhi kaun kaun chal raha hai?)
  → Transaction ACTIVE mark hoti hai

... kaam karo (INSERT, UPDATE, DELETE) ...

COMMIT
  → Txn ko COMMITTED mark karo MVCC mein
  → Modified tables disk pe save karo
  → Savepoints clear karo

  -- YA --

ROLLBACK
  → Saare changes undo karo:
     - Jo rows MAINE banaayi (_xmin == meri txn_id) → hatao
     - Jo rows MAINE delete ki (_xmax == meri txn_id) → wapas laao (_xmax = 0)
  → Txn ko ABORTED mark karo
```

### Savepoints — Aadha Rollback

Savepoints se tum transaction ka ek PART undo kar sakte ho, poora nahi:

```sql
BEGIN;
INSERT INTO employees VALUES (100, 'Test1', 25, 50000, 'HR');

SAVEPOINT sp1;  -- yahan ek checkpoint banaya
INSERT INTO employees VALUES (101, 'Test2', 30, 60000, 'Sales');
-- Oops, galti ho gayi

ROLLBACK TO sp1;  -- sp1 tak wapas jaao
-- Test2 hat gaya, par Test1 abhi bhi hai!

COMMIT;
-- Sirf Test1 save hoga
```

**Internally kaise kaam karta hai:**
1. `SAVEPOINT sp1` — abhi ki rows ka snapshot le lo
2. `ROLLBACK TO sp1` — rows ko us snapshot state mein restore karo
3. `RELEASE SAVEPOINT sp1` — snapshot hata do (ab wapas nahi jaa sakte)

---

## MVCC — Ek Saath Multiple Transactions

MVCC allow karta hai ki **multiple transactions ek saath read aur write karein** bina ek doosre ko block kiye.

### Main Idea

Rows ko LOCK karne ki jagah, MVCC **multiple versions** rakhta hai har row ki. Har transaction apna consistent snapshot dekhta hai.

### Har Row Mein 2 Hidden Columns

| Column | Matlab |
|--------|--------|
| `_xmin` | Kaunsi transaction ne ye row **CREATE kiya** |
| `_xmax` | Kaunsi transaction ne ye row **DELETE kiya** (0 = abhi alive) |

### Operations Kaise Kaam Karti Hain MVCC Ke Saath

**INSERT:**
```
Naya row → _xmin = meri txn_id, _xmax = 0 (zinda hai)
```

**DELETE:**
```
Row ko physically NAHI hatate!
Sirf _xmax = meri txn_id set karte hain
Row ab "invisible" ho jaata hai future transactions ko
```

**UPDATE:**
```
= DELETE purana version + INSERT naya version
Purani row → _xmax = meri txn_id (dead mark)
Nayi row → _xmin = meri txn_id, _xmax = 0 (naya version)
```

### Visibility Rule — Row Dikhega Ya Nahi?

Jab Transaction T ek row padhti hai:

```
Row DIKHEGA agar:
  1. _xmin COMMITTED hai (ya _xmin == T khud)
     AUR
  2. _xmax == 0 (kisi ne delete nahi kiya)
     YA _xmax COMMITTED NAHI hai aur _xmax != T
```

### Snapshot Isolation

Jab transaction BEGIN hota hai, ek **snapshot** liya jaata hai — us waqt ka frozen view.

```
Snapshot = {
    xmin: 5,              # meri transaction ID
    active_txns: {3, 4}   # ye txns chal rahi thi jab maine start kiya
}
```

Iska matlab:
- Mujhe txn 1, 2 ki rows dikhengi (mere se pehle committed)
- Mujhe txn 3, 4 ki rows NAHI dikhengi (abhi chal rahi hain)
- Mujhe txn 6 ki rows NAHI dikhengi (mere baad start hui)
- Mujhe MERI khud ki rows dikhengi (txn 5)

### Visual Example — Timeline Se Samjho

```
Time →
                T1          T2          T3
                |           |           |
         BEGIN  |    BEGIN  |           |
                |           |           |
    INSERT(Alice, _xmin=1)  |           |
                |           |           |
         COMMIT |    SELECT * ──→ Alice dikhegi (T1 committed hai)
                |           |           |
                |  DELETE(Alice, _xmax=2) |   BEGIN
                |           |           |
                |    COMMIT |    SELECT * ──→ Alice NAHI dikhegi
                |           |           |       (_xmax=2 committed hai)
```

### Vacuum — Safai

Time ke saath dead rows pile up hoti hain (delete pe physically nahi hatti):

```sql
VACUUM;
```

Ye remove karta hai wo rows jinke:
- `_xmin` committed hai AUR `_xmax` bhi committed hai
- Koi bhi active transaction unhe dekh nahi sakti ab

Socho jaise ghar ki safai — jo cheezein kisi ke kaam ki nahi rahi, unhe phek do.

---

## ACID Properties — MiniDB Kaise Achieve Karta Hai

| Property | Kaise |
|----------|-------|
| **Atomicity** (Sab ya kuch nahi) | WAL + ROLLBACK se saare changes undo ho jaate hain |
| **Consistency** (Constraints follow) | PK, FK, UNIQUE, NOT NULL, CHECK constraints check hote hain |
| **Isolation** (Transactions independent) | MVCC snapshots — har txn apna consistent view dekhti hai |
| **Durability** (Commit = permanent) | WAL pehle disk pe likhta hai; crash pe recovery possible |
