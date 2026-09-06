# Parser (parser/)

Parser ka kaam hai **raw SQL text** ko ek structured **tree (AST)** mein convert karna jo executor samajh sake. Ye 2 steps mein hota hai: Lexer (tokenize) aur Parser (tree banao).

## Files Involved
- `parser/lexer.py` — Tokenizer (SQL text → tokens)
- `parser/ast.py` — AST node definitions (dataclasses)
- `parser/parser.py` — Recursive descent parser (tokens → AST)

---

## Step 1: Lexer (lexer.py) — SQL Ko Todna

Lexer SQL string ko chhote chhote pieces (tokens) mein todta hai — jaise ek sentence ko words mein todte hain.

**Input:**
```sql
SELECT name, salary FROM employees WHERE age > 25
```

**Output (tokens):**
```python
["SELECT", "name", ",", "salary", "FROM", "employees", "WHERE", "age", ">", "25"]
```

**Kya kya handle karta hai:**
- Keywords (SELECT, FROM, WHERE, INSERT, etc.)
- Identifiers (table/column ke names)
- Numbers (42, 3.14)
- Strings (single-quoted: `'hello'`)
- Operators (`=`, `!=`, `<`, `>`, `<=`, `>=`)
- Punctuation (`(`, `)`, `,`, `*`)
- Comments aur semicolons hata deta hai

Internally regex use karta hai — ek pattern se match karke tokens extract karta hai.

---

## Step 2: AST Nodes (ast.py) — SQL Ka Matlab

AST = Abstract Syntax Tree. Ye Python dataclasses hain — simple containers jo SQL ka structure represent karte hain.

Socho jaise:
- SQL sentence hai: "Get name from employees where age is more than 25"
- AST uska meaning hai: table=employees, columns=[name], filter=age>25

**DML nodes (data operations):**
- `SelectNode` — columns, table, joins, where, group_by, having, order_by, limit
- `InsertNode` — table, columns, values (multi-row bhi support karta hai)
- `UpdateNode` — table, assignments (kya badalna hai), where
- `DeleteNode` — table, where

**DDL nodes (structure operations):**
- `CreateTableNode` — table name, column definitions
- `DropTableNode`, `TruncateNode`, `RenameTableNode`
- `AlterAddColumnNode`, `AlterDropColumnNode`

**Transaction nodes:**
- `BeginNode`, `CommitNode`, `RollbackNode`
- `SavepointNode`, `RollbackToSavepointNode`

**Utility nodes:**
- `ShowTablesNode`, `DescribeNode`, `RecoverNode`, `VacuumNode`

---

## Step 3: Parser (parser.py) — Tokens Se Tree Banana

Ye ek **recursive descent parser** hai — tokens ko left se right padhta hai aur first token dekh ke decide karta hai kaunsa method call karna hai.

**Kaise kaam karta hai:**
```
parse(sql)
  → lexer.tokenize(sql)     → ["SELECT", "name", ...]
  → pehla token dekho
  → "SELECT"? → _parse_select() call karo
  → "INSERT"? → _parse_insert() call karo
  → "CREATE"? → _parse_create_table() ya _parse_create_index()
  → aur bhi...
```

**SELECT parsing ka flow:**
```
_parse_select()
  1. Column list parse karo (*, aliases, aggregates jaise COUNT(col))
  2. FROM keyword expect karo
  3. Table name parse karo
  4. Optional: JOINs parse karo (INNER, LEFT, RIGHT, FULL, CROSS)
  5. Optional: WHERE clause parse karo (_parse_where)
  6. Optional: GROUP BY columns parse karo
  7. Optional: HAVING clause parse karo
  8. Optional: ORDER BY columns parse karo (ASC/DESC ke saath)
  9. Optional: LIMIT number parse karo
  → SelectNode return karo with sabke details
```

**WHERE parsing (_parse_where) — sabse complex part:**

Ye handle karta hai:
- Simple comparisons: `age > 25`
- AND/OR: `age > 25 AND department = 'Engineering'`
- NOT: `NOT status = 'cancelled'`
- BETWEEN: `salary BETWEEN 50000 AND 100000`
- IN: `department IN ('Engineering', 'Sales')`
- IS NULL / IS NOT NULL
- LIKE: `name LIKE 'A%'`
- Subqueries: `id IN (SELECT employee_id FROM orders)`
- Parenthesized groups: `(age > 25 OR age < 20) AND department = 'HR'`

**BETWEEN ka trick:**
Parser `col BETWEEN x AND y` ko pehle `col >= x AND col <= y` mein rewrite karta hai BEFORE WHERE parse karne se. Kyu? Kyunki BETWEEN ke andar wala AND, conditions join karne wale AND se confuse ho jaata tha.

---

## Poora Example

```sql
SELECT name, salary FROM employees WHERE department = 'Engineering' AND salary > 100000 ORDER BY salary DESC LIMIT 10
```

**Lexing ke baad:**
```python
["SELECT", "name", ",", "salary", "FROM", "employees", "WHERE",
 "department", "=", "'Engineering'", "AND", "salary", ">", "100000",
 "ORDER", "BY", "salary", "DESC", "LIMIT", "10"]
```

**Parsing ke baad → SelectNode:**
```python
SelectNode(
    columns=["name", "salary"],
    table="employees",
    where=["department", "=", "Engineering", "AND", "salary", ">", 100000],
    order_by=[("salary", "DESC")],
    limit=10,
    joins=[],
    group_by=[],
    having=None
)
```

Ye AST node ab Executor ko jaayega actual execution ke liye.

Socho jaise — SQL ek Hindi sentence hai, Lexer use words mein todta hai, Parser use samajh ke ek form bhar deta hai jisme likha hai "kya chahiye, kahan se, kaunsi condition". Executor wo form padh ke kaam karta hai.
