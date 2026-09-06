Project Structure
mini_db/
│
├── storage/
│   ├── engine.py
│   ├── page.py
│   ├── wal.py
│   └── buffer_pool.py
│
├── parser/
│   ├── lexer.py
│   ├── parser.py
│   └── ast.py
│
├── executor/
│   ├── executor.py
│   └── planner.py
│
├── index/
│   └── bplustree.py
│
├── transaction/
│   └── manager.py
│
├── tests/
│
├── data/
│
└── main.py
Phase 1 — Build a Persistent Key-Value Store
Goal

Support:

SET name john
GET name
DELETE name
Step 1: Create Storage Engine

storage/engine.py

class StorageEngine:

    def __init__(self):
        self.data = {}

    def set(self, key, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)
Step 2: CLI

main.py

from storage.engine import StorageEngine

db = StorageEngine()

while True:

    command = input("db > ")

    parts = command.split()

    if parts[0] == "SET":
        db.set(parts[1], parts[2])

    elif parts[0] == "GET":
        print(db.get(parts[1]))

    elif parts[0] == "DELETE":
        db.delete(parts[1])

    elif parts[0] == "EXIT":
        break

Run:

python main.py
Step 3: Add Persistence

Database should survive restart.

Create:

data/database.log
Write Operations

Every SET:

SET name john

append to file.

with open("data/database.log", "a") as f:
    f.write(f"SET {key} {value}\n")
Recovery

When DB starts:

def recover(self):

Read file.

Replay every command.

SET name john
SET city delhi
DELETE city

Result:

{
    "name":"john"
}
Phase 2 — Build Tables

Current:

SET user1 john

Need:

CREATE TABLE users
Table Model
class Table:

    def __init__(self,name,columns):
        self.name=name
        self.columns=columns
        self.rows=[]

Example

users = Table(
    "users",
    ["id","name"]
)
Insert Row
users.rows.append(
    {
      "id":1,
      "name":"John"
    }
)

Support:

INSERT users 1 John
Phase 3 — Store Tables on Disk

Current:

rows=[]

Everything disappears.

Need:

users.tbl

Example file:

[
  {
     "id":1,
     "name":"john"
  }
]
Save
import json

json.dump(rows,file)
Load
rows = json.load(file)

At this stage:

✔ persistent

✔ tables

✔ inserts

✔ reads

Phase 4 — SQL Parser

Current:

INSERT users 1 john

Need:

INSERT INTO users VALUES (1,'john')
Lexer

Input:

SELECT * FROM users

Output:

[
 'SELECT',
 '*',
 'FROM',
 'users'
]

Create:

class Lexer:

Method:

tokenize()
AST

Example:

SELECT * FROM users

becomes:

SelectNode(
  table="users"
)

File:

parser/ast.py
@dataclass
class SelectNode:
    table:str
Phase 5 — Query Executor

Pipeline:

SQL
 ↓
Lexer
 ↓
Parser
 ↓
AST
 ↓
Executor

Executor:

def execute(node):

If:

SelectNode

then:

table.scan()
Phase 6 — Real Storage Pages

Currently:

JSON

Not database-like.

Need pages.

Page Size

4096 bytes

Create:

class Page:
PAGE_SIZE = 4096

self.data = bytearray(PAGE_SIZE)

Store rows inside pages.

Exactly how real databases operate.

Phase 7 — Build a B+ Tree

This is where interview value jumps significantly.

Without Index

SELECT *
FROM users
WHERE id=1000

Scan all rows.

Complexity:

O(n)

With Index

O(log n)

Create

index/bplustree.py

Implement:

insert()
search()
split()
delete()

Structure

            50
          /    \
       20       80
      /  \     /  \

Use order:

ORDER = 4

Initially.

Phase 8 — Query Planner

Current:

scan()

always.

Need:

if indexed_column:

use:

index.search()

otherwise:

scan()
Phase 9 — WAL

Critical interview topic.

Create:

data/wal.log

Before every insert:

INSERT user 1

Write:

LSN=101
INSERT user 1

to WAL.

Then update table.

Recovery:

read wal
replay commands

This demonstrates:

Atomicity
Durability

from ACID.

Phase 10 — Transactions

Support:

BEGIN;
INSERT ...
UPDATE ...
COMMIT;

Transaction Object

class Transaction:
id
operations
state

States

ACTIVE

COMMITTED

ABORTED
Phase 11 — Concurrency

Support:

100 threads

Use:

import threading

Table Lock

lock = threading.RLock()

Write

with lock:

Read

with read_lock:

(you can implement a read-write lock yourself).

Phase 12 — Buffer Pool

Current:

Disk read every query

Slow.

Build cache.

class BufferPool:

Use:

OrderedDict

for LRU.

Benefits:

Page Hit
 ↓
Memory

Page Miss
 ↓
Disk
Suggested Timeline
Week 1
KV Store
Persistence
CLI
Week 2
Tables
Inserts
Select
Week 3
SQL Parser
AST
Executor
Week 4
Page Storage
Week 5
B+ Tree
Week 6
WAL
Week 7
Transactions
Week 8
Concurrency
Buffer Pool

For maximum resume value, publish each phase as a GitHub release and write a design document explaining:

Storage format
Page layout
B+ tree implementation
WAL recovery algorithm
Transaction model
Locking strategy

That documentation is often what interviewers discuss, because it shows you understand the tradeoffs, not just the code.