"""
MiniDB Test Suite  —  tests/test_minidb.py

Run:  pytest tests/ -v

Each test class is self-contained: it creates a fresh StorageEngine
in a temporary directory so tests do not interfere with each other or
with the real data/ directory.

Coverage
─────────────────────────────────────────────────────────────────────
  TestDDL          — CREATE / DROP / ALTER / RENAME / TRUNCATE
  TestDML          — INSERT / SELECT / UPDATE / DELETE, WHERE, ORDER,
                     LIMIT, OFFSET
  TestAggregates   — COUNT, SUM, AVG, MIN, MAX, GROUP BY, HAVING
  TestIndex        — B+ tree exact lookup + Mod 4c range scan
  TestBETWEEN      — Mod 4b BETWEEN … AND … in WHERE
  TestJoins        — INNER / LEFT / RIGHT / CROSS (Mod 6)
  TestSubqueries   — Mod 5: col IN (SELECT …)
  TestTransactions — BEGIN / COMMIT / ROLLBACK
  TestSavepoints   — Mod 3: SAVEPOINT / ROLLBACK TO / RELEASE
  TestWAL          — Mod 2: WAL recovery replays INSERT+UPDATE+DELETE
  TestViews        — CREATE VIEW / DROP VIEW / query via view
  TestConstraints  — NOT NULL, UNIQUE, PRIMARY KEY, CHECK, FK
"""

import os
import sys
import shutil
import tempfile
import pytest

# Ensure the project root is on sys.path so imports work
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from storage.engine import StorageEngine
from storage.column import Column
from parser.parser  import Parser, ParseError
from executor.executor import Executor


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_engine(tmp_path, monkeypatch):
    """
    A fresh StorageEngine that stores data under a temporary directory.
    Isolated from the real data/ directory and from other tests.
    """
    monkeypatch.chdir(tmp_path)
    engine = StorageEngine()
    yield engine
    # cleanup is handled by pytest's tmp_path fixture


@pytest.fixture
def db(tmp_engine):
    """Return (engine, parser, executor) ready to run SQL."""
    parser   = Parser()
    executor = Executor(tmp_engine)

    def run(sql: str):
        node = parser.parse(sql)
        return executor.execute(node)

    return tmp_engine, run


# ─────────────────────────────────────────────────────────────────────────────
# DDL
# ─────────────────────────────────────────────────────────────────────────────

class TestDDL:

    def test_create_table(self, db):
        engine, run = db
        run("CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)")
        assert "users" in engine.tables
        assert len(engine.tables["users"].columns) == 2

    def test_drop_table(self, db):
        engine, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("DROP TABLE t")
        assert "t" not in engine.tables

    def test_drop_table_if_exists(self, db):
        _, run = db
        run("DROP TABLE IF EXISTS nonexistent")  # should not raise

    def test_truncate(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("INSERT INTO t VALUES (1)")
        run("INSERT INTO t VALUES (2)")
        result = run("TRUNCATE t")
        assert "2" in result["message"]
        assert run("SELECT * FROM t")["rows"] == []

    def test_rename_table(self, db):
        engine, run = db
        run("CREATE TABLE old (id INT PRIMARY KEY)")
        run("RENAME TABLE old TO new")
        assert "old" not in engine.tables
        assert "new" in engine.tables

    def test_alter_add_column(self, db):
        engine, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("ALTER TABLE t ADD COLUMN age INT DEFAULT 0")
        col_names = [c.name for c in engine.tables["t"].columns]
        assert "age" in col_names

    def test_alter_drop_column(self, db):
        engine, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        run("ALTER TABLE t DROP COLUMN name")
        col_names = [c.name for c in engine.tables["t"].columns]
        assert "name" not in col_names

    def test_alter_rename_column(self, db):
        engine, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, nm TEXT)")
        run("ALTER TABLE t RENAME COLUMN nm TO name")
        col_names = [c.name for c in engine.tables["t"].columns]
        assert "nm" not in col_names
        assert "name" in col_names


# ─────────────────────────────────────────────────────────────────────────────
# DML
# ─────────────────────────────────────────────────────────────────────────────

class TestDML:

    def setup_users(self, run):
        run("CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)")
        run("INSERT INTO users VALUES (1, 'Alice', 30)")
        run("INSERT INTO users VALUES (2, 'Bob', 25)")
        run("INSERT INTO users VALUES (3, 'Carol', 35)")

    def test_insert_and_select_all(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users")["rows"]
        assert len(rows) == 3

    def test_select_columns(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT name FROM users")["rows"]
        assert all("age" not in r for r in rows)
        assert rows[0]["name"] in {"Alice", "Bob", "Carol"}

    def test_where_equality(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users WHERE id = 2")["rows"]
        assert len(rows) == 1
        assert rows[0]["name"] == "Bob"

    def test_where_gt(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users WHERE age > 29")["rows"]
        assert all(r["age"] > 29 for r in rows)

    def test_where_and(self, db):
        _, run = db
        self.setup_users(run)
        # Alice=30, Bob=25, Carol=35.  age > 29 AND age < 31 → only Alice.
        rows = run("SELECT * FROM users WHERE age > 29 AND age < 31")["rows"]
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice"

    def test_where_like(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users WHERE name LIKE 'A%'")["rows"]
        assert len(rows) == 1 and rows[0]["name"] == "Alice"

    def test_where_in(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users WHERE id IN (1, 3)")["rows"]
        assert {r["id"] for r in rows} == {1, 3}

    def test_order_by_asc(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users ORDER BY age ASC")["rows"]
        ages = [r["age"] for r in rows]
        assert ages == sorted(ages)

    def test_order_by_desc(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users ORDER BY age DESC")["rows"]
        ages = [r["age"] for r in rows]
        assert ages == sorted(ages, reverse=True)

    def test_limit(self, db):
        _, run = db
        self.setup_users(run)
        rows = run("SELECT * FROM users LIMIT 2")["rows"]
        assert len(rows) == 2

    def test_offset(self, db):
        _, run = db
        self.setup_users(run)
        rows_all    = run("SELECT * FROM users ORDER BY id ASC")["rows"]
        rows_offset = run("SELECT * FROM users ORDER BY id ASC LIMIT 10 OFFSET 1")["rows"]
        assert rows_offset == rows_all[1:]

    def test_update(self, db):
        _, run = db
        self.setup_users(run)
        run("UPDATE users SET age = 99 WHERE id = 1")
        rows = run("SELECT * FROM users WHERE id = 1")["rows"]
        assert rows[0]["age"] == 99

    def test_delete(self, db):
        _, run = db
        self.setup_users(run)
        run("DELETE FROM users WHERE id = 2")
        rows = run("SELECT * FROM users")["rows"]
        assert all(r["id"] != 2 for r in rows)
        assert len(rows) == 2

    def test_is_null(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, note TEXT)")
        run("INSERT INTO t (id) VALUES (1)")
        rows = run("SELECT * FROM t WHERE note IS NULL")["rows"]
        assert len(rows) == 1

    def test_is_not_null(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, note TEXT)")
        run("INSERT INTO t VALUES (1, 'hi')")
        run("INSERT INTO t (id) VALUES (2)")
        rows = run("SELECT * FROM t WHERE note IS NOT NULL")["rows"]
        assert len(rows) == 1 and rows[0]["note"] == "hi"


# ─────────────────────────────────────────────────────────────────────────────
# Aggregates
# ─────────────────────────────────────────────────────────────────────────────

class TestAggregates:

    def setup_scores(self, run):
        run("CREATE TABLE scores (id INT PRIMARY KEY, dept TEXT, score FLOAT)")
        for i, (dept, score) in enumerate(
            [("eng", 80), ("eng", 90), ("hr", 70), ("hr", 60)], 1
        ):
            run(f"INSERT INTO scores VALUES ({i}, '{dept}', {score})")

    def test_count(self, db):
        _, run = db
        self.setup_scores(run)
        rows = run("SELECT COUNT(*) FROM scores")["rows"]
        assert rows[0]["COUNT(*)"] == 4

    def test_sum(self, db):
        _, run = db
        self.setup_scores(run)
        rows = run("SELECT SUM(score) FROM scores")["rows"]
        assert rows[0]["SUM(score)"] == 300.0

    def test_avg(self, db):
        _, run = db
        self.setup_scores(run)
        rows = run("SELECT AVG(score) FROM scores")["rows"]
        assert rows[0]["AVG(score)"] == 75.0

    def test_min_max(self, db):
        _, run = db
        self.setup_scores(run)
        rows = run("SELECT MIN(score), MAX(score) FROM scores")["rows"]
        assert rows[0]["MIN(score)"] == 60
        assert rows[0]["MAX(score)"] == 90

    def test_group_by(self, db):
        _, run = db
        self.setup_scores(run)
        rows = run("SELECT dept, COUNT(*) FROM scores GROUP BY dept")["rows"]
        by_dept = {r["dept"]: r["COUNT(*)"] for r in rows}
        assert by_dept == {"eng": 2, "hr": 2}

    def test_having(self, db):
        _, run = db
        self.setup_scores(run)
        rows = run(
            "SELECT dept, AVG(score) FROM scores "
            "GROUP BY dept HAVING AVG(score) > 75"
        )["rows"]
        assert len(rows) == 1
        assert rows[0]["dept"] == "eng"


# ─────────────────────────────────────────────────────────────────────────────
# Index: exact + range scan  (Mod 4c)
# ─────────────────────────────────────────────────────────────────────────────

class TestIndex:

    def setup_indexed(self, run):
        run("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
        run("CREATE INDEX idx_val ON t(val)")
        for i in range(1, 11):
            run(f"INSERT INTO t VALUES ({i}, {i * 10})")

    def test_exact_index_scan(self, db):
        _, run = db
        self.setup_indexed(run)
        result = run("SELECT * FROM t WHERE val = 50")
        assert result["rows"][0]["id"] == 5
        assert "index" in result["plan"].lower()

    def test_range_scan_gte(self, db):
        _, run = db
        self.setup_indexed(run)
        result = run("SELECT * FROM t WHERE val >= 70")
        vals = {r["val"] for r in result["rows"]}
        assert vals == {70, 80, 90, 100}
        assert "range" in result["plan"].lower()

    def test_range_scan_lte(self, db):
        _, run = db
        self.setup_indexed(run)
        result = run("SELECT * FROM t WHERE val <= 30")
        vals = {r["val"] for r in result["rows"]}
        assert vals == {10, 20, 30}

    def test_range_scan_gt_lt(self, db):
        _, run = db
        self.setup_indexed(run)
        result = run("SELECT * FROM t WHERE val > 30 AND val < 70")
        vals = {r["val"] for r in result["rows"]}
        assert vals == {40, 50, 60}

    def test_range_scan_compound(self, db):
        _, run = db
        self.setup_indexed(run)
        result = run("SELECT * FROM t WHERE val >= 20 AND val <= 50")
        vals = sorted(r["val"] for r in result["rows"])
        assert vals == [20, 30, 40, 50]

    def test_unique_index_violation(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("CREATE UNIQUE INDEX u_id ON t(id)")
        run("INSERT INTO t VALUES (1)")
        with pytest.raises(Exception):
            run("INSERT INTO t VALUES (1)")


# ─────────────────────────────────────────────────────────────────────────────
# BETWEEN  (Mod 4b)
# ─────────────────────────────────────────────────────────────────────────────

class TestBETWEEN:

    def test_between_no_index(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        for i in range(1, 6):
            run(f"INSERT INTO t VALUES ({i}, {i * 10})")
        rows = run("SELECT * FROM t WHERE age BETWEEN 20 AND 40")["rows"]
        ages = sorted(r["age"] for r in rows)
        assert ages == [20, 30, 40]

    def test_between_with_index(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        run("CREATE INDEX idx_age ON t(age)")
        for i in range(1, 6):
            run(f"INSERT INTO t VALUES ({i}, {i * 10})")
        result = run("SELECT * FROM t WHERE age BETWEEN 20 AND 40")
        ages = sorted(r["age"] for r in result["rows"])
        assert ages == [20, 30, 40]


# ─────────────────────────────────────────────────────────────────────────────
# JOINs  (Mod 6 adds CROSS)
# ─────────────────────────────────────────────────────────────────────────────

class TestJoins:

    def setup_tables(self, run):
        run("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, dept_id INT)")
        run("CREATE TABLE depts (id INT PRIMARY KEY, dept_name TEXT)")
        run("INSERT INTO users VALUES (1, 'Alice', 10)")
        run("INSERT INTO users VALUES (2, 'Bob', 20)")
        run("INSERT INTO users VALUES (3, 'Carol', 99)")  # no matching dept
        run("INSERT INTO depts VALUES (10, 'Engineering')")
        run("INSERT INTO depts VALUES (20, 'HR')")

    def test_inner_join(self, db):
        _, run = db
        self.setup_tables(run)
        rows = run(
            "SELECT users.name, depts.dept_name "
            "FROM users JOIN depts ON users.dept_id = depts.id"
        )["rows"]
        assert len(rows) == 2  # Carol has no match

    def test_left_join(self, db):
        _, run = db
        self.setup_tables(run)
        rows = run(
            "SELECT users.name, depts.dept_name "
            "FROM users LEFT JOIN depts ON users.dept_id = depts.id"
        )["rows"]
        assert len(rows) == 3  # Carol included with NULL dept

    def test_right_join(self, db):
        _, run = db
        self.setup_tables(run)
        rows = run(
            "SELECT users.name, depts.dept_name "
            "FROM users RIGHT JOIN depts ON users.dept_id = depts.id"
        )["rows"]
        assert len(rows) == 2  # only Engineering and HR (with matched users)

    def test_cross_join(self, db):
        """Mod 6: CROSS JOIN produces Cartesian product (no ON clause)."""
        _, run = db
        self.setup_tables(run)
        rows = run(
            "SELECT users.name, depts.dept_name "
            "FROM users CROSS JOIN depts"
        )["rows"]
        # 3 users × 2 depts = 6 rows
        assert len(rows) == 6


# ─────────────────────────────────────────────────────────────────────────────
# Subqueries  (Mod 5)
# ─────────────────────────────────────────────────────────────────────────────

class TestSubqueries:

    def setup_tables(self, run):
        run("CREATE TABLE depts (id INT PRIMARY KEY, name TEXT, active INT)")
        run("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, dept_id INT)")
        run("INSERT INTO depts VALUES (1, 'Eng', 1)")
        run("INSERT INTO depts VALUES (2, 'HR', 0)")
        run("INSERT INTO depts VALUES (3, 'Sales', 1)")
        run("INSERT INTO users VALUES (1, 'Alice', 1)")
        run("INSERT INTO users VALUES (2, 'Bob', 2)")
        run("INSERT INTO users VALUES (3, 'Carol', 3)")

    def test_subquery_in_where(self, db):
        """WHERE dept_id IN (SELECT id FROM depts WHERE active = 1)"""
        _, run = db
        self.setup_tables(run)
        rows = run(
            "SELECT name FROM users WHERE dept_id IN "
            "(SELECT id FROM depts WHERE active = 1)"
        )["rows"]
        names = {r["name"] for r in rows}
        assert names == {"Alice", "Carol"}

    def test_subquery_empty_result(self, db):
        """Subquery returns no rows → main query returns no rows."""
        _, run = db
        self.setup_tables(run)
        rows = run(
            "SELECT name FROM users WHERE dept_id IN "
            "(SELECT id FROM depts WHERE active = 99)"
        )["rows"]
        assert rows == []

    def test_subquery_in_delete(self, db):
        """DELETE … WHERE col IN (SELECT …)"""
        _, run = db
        self.setup_tables(run)
        run(
            "DELETE FROM users WHERE dept_id IN "
            "(SELECT id FROM depts WHERE active = 0)"
        )
        rows = run("SELECT * FROM users")["rows"]
        assert all(r["name"] != "Bob" for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# Transactions
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactions:

    def test_commit(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("BEGIN")
        run("INSERT INTO t VALUES (1)")
        run("COMMIT")
        assert len(run("SELECT * FROM t")["rows"]) == 1

    def test_rollback(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("BEGIN")
        run("INSERT INTO t VALUES (1)")
        run("ROLLBACK")
        assert run("SELECT * FROM t")["rows"] == []

    def test_double_begin_raises(self, db):
        _, run = db
        run("BEGIN")
        with pytest.raises(Exception):
            run("BEGIN")
        run("ROLLBACK")


# ─────────────────────────────────────────────────────────────────────────────
# Savepoints  (Mod 3)
# ─────────────────────────────────────────────────────────────────────────────

class TestSavepoints:

    def test_savepoint_rollback_to(self, db):
        """
        BEGIN
          INSERT 1
          SAVEPOINT sp1
          INSERT 2
          ROLLBACK TO sp1   ← row 2 gone, row 1 kept
        COMMIT
        """
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("BEGIN")
        run("INSERT INTO t VALUES (1)")
        run("SAVEPOINT sp1")
        run("INSERT INTO t VALUES (2)")
        run("ROLLBACK TO SAVEPOINT sp1")
        run("COMMIT")
        rows = run("SELECT * FROM t")["rows"]
        assert len(rows) == 1
        assert rows[0]["id"] == 1

    def test_savepoint_continue_after_rollback(self, db):
        """After ROLLBACK TO sp, more work can be done and committed."""
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("BEGIN")
        run("INSERT INTO t VALUES (1)")
        run("SAVEPOINT sp1")
        run("INSERT INTO t VALUES (2)")
        run("ROLLBACK TO sp1")
        run("INSERT INTO t VALUES (3)")  # this should stay
        run("COMMIT")
        ids = {r["id"] for r in run("SELECT * FROM t")["rows"]}
        assert ids == {1, 3}

    def test_release_savepoint(self, db):
        """RELEASE removes the savepoint name."""
        engine, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("BEGIN")
        run("SAVEPOINT sp1")
        run("RELEASE SAVEPOINT sp1")
        with pytest.raises(Exception):
            run("ROLLBACK TO SAVEPOINT sp1")
        run("ROLLBACK")

    def test_multiple_savepoints(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("BEGIN")
        run("INSERT INTO t VALUES (1)")
        run("SAVEPOINT sp1")
        run("INSERT INTO t VALUES (2)")
        run("SAVEPOINT sp2")
        run("INSERT INTO t VALUES (3)")
        run("ROLLBACK TO SAVEPOINT sp2")  # lose 3
        run("ROLLBACK TO SAVEPOINT sp1")  # lose 2
        run("COMMIT")
        ids = {r["id"] for r in run("SELECT * FROM t")["rows"]}
        assert ids == {1}


# ─────────────────────────────────────────────────────────────────────────────
# WAL Recovery  (Mod 2)
# ─────────────────────────────────────────────────────────────────────────────

class TestWAL:

    def test_recovery_replays_insert(self, tmp_path, monkeypatch):
        """INSERT is replayed from WAL after simulated restart."""
        monkeypatch.chdir(tmp_path)
        engine1 = StorageEngine()
        p, e    = Parser(), Executor(engine1)

        def run1(sql):
            return e.execute(p.parse(sql))

        run1("CREATE TABLE t (id INT PRIMARY KEY)")
        run1("INSERT INTO t VALUES (1)")
        # Simulate crash: do NOT call checkpoint → WAL still has the record

        # "Restart" with a fresh engine in the same directory
        engine2  = StorageEngine()
        p2, e2   = Parser(), Executor(engine2)

        def run2(sql):
            return e2.execute(p2.parse(sql))

        run2("RECOVER")
        rows = run2("SELECT * FROM t")["rows"]
        assert any(r["id"] == 1 for r in rows)

    def test_recovery_replays_update(self, tmp_path, monkeypatch):
        """UPDATE is replayed from WAL (Mod 2)."""
        monkeypatch.chdir(tmp_path)
        engine1 = StorageEngine()
        p, e    = Parser(), Executor(engine1)

        def run1(sql):
            return e.execute(p.parse(sql))

        run1("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
        run1("INSERT INTO t VALUES (1, 10)")
        # Manually flush so the table file has the initial insert
        engine1._flush_all()
        run1("UPDATE t SET val = 99 WHERE id = 1")
        # Don't checkpoint → WAL has the UPDATE record

        engine2  = StorageEngine()
        p2, e2   = Parser(), Executor(engine2)

        def run2(sql):
            return e2.execute(p2.parse(sql))

        run2("RECOVER")
        rows = run2("SELECT * FROM t WHERE id = 1")["rows"]
        assert rows[0]["val"] == 99


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────

class TestViews:

    def test_create_and_query_view(self, db):
        _, run = db
        run("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, active INT)")
        run("INSERT INTO users VALUES (1, 'Alice', 1)")
        run("INSERT INTO users VALUES (2, 'Bob', 0)")
        run("CREATE VIEW active_users AS SELECT * FROM users WHERE active = 1")
        rows = run("SELECT * FROM active_users")["rows"]
        assert len(rows) == 1 and rows[0]["name"] == "Alice"

    def test_drop_view(self, db):
        engine, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("CREATE VIEW v AS SELECT * FROM t")
        run("DROP VIEW v")
        assert "v" not in engine.views

    def test_show_views(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("CREATE VIEW v AS SELECT * FROM t")
        result = run("SHOW VIEWS")
        assert "v" in result["list"]


# ─────────────────────────────────────────────────────────────────────────────
# Constraints
# ─────────────────────────────────────────────────────────────────────────────

class TestConstraints:

    def test_not_null(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
        with pytest.raises(Exception):
            run("INSERT INTO t VALUES (1, NULL)")

    def test_primary_key_unique(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY)")
        run("INSERT INTO t VALUES (1)")
        with pytest.raises(Exception):
            run("INSERT INTO t VALUES (1)")

    def test_check_constraint(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, age INT CHECK(> 0))")
        with pytest.raises(Exception):
            run("INSERT INTO t VALUES (1, -1)")

    def test_foreign_key(self, db):
        _, run = db
        run("CREATE TABLE depts (id INT PRIMARY KEY)")
        run("CREATE TABLE users (id INT PRIMARY KEY, dept_id INT REFERENCES depts(id))")
        run("INSERT INTO depts VALUES (1)")
        with pytest.raises(Exception):
            run("INSERT INTO users VALUES (1, 99)")   # 99 not in depts

    def test_default_value(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, score INT DEFAULT 0)")
        run("INSERT INTO t (id) VALUES (1)")
        rows = run("SELECT * FROM t WHERE id = 1")["rows"]
        assert rows[0]["score"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# DISTINCT
# ─────────────────────────────────────────────────────────────────────────────

class TestDistinct:

    def test_distinct_removes_duplicates(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, dept TEXT)")
        run("INSERT INTO t VALUES (1, 'eng')")
        run("INSERT INTO t VALUES (2, 'eng')")
        run("INSERT INTO t VALUES (3, 'hr')")
        rows = run("SELECT DISTINCT dept FROM t")["rows"]
        depts = [r["dept"] for r in rows]
        assert sorted(depts) == ["eng", "hr"]

    def test_distinct_all_unique(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        run("INSERT INTO t VALUES (1, 'Alice')")
        run("INSERT INTO t VALUES (2, 'Bob')")
        rows = run("SELECT DISTINCT name FROM t")["rows"]
        assert len(rows) == 2

    def test_distinct_star(self, db):
        """DISTINCT on * deduplicates entire rows."""
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
        run("INSERT INTO t VALUES (1, 10)")
        run("INSERT INTO t VALUES (2, 10)")
        # With *, rows differ on id, so both should remain
        rows = run("SELECT DISTINCT * FROM t")["rows"]
        assert len(rows) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Multi-row INSERT
# ─────────────────────────────────────────────────────────────────────────────

class TestMultiRowInsert:

    def test_multi_row_insert(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        result = run("INSERT INTO t VALUES (1, 'Alice'), (2, 'Bob'), (3, 'Carol')")
        assert result["count"] == 3
        rows = run("SELECT * FROM t")["rows"]
        assert len(rows) == 3

    def test_multi_row_insert_with_columns(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT, score INT DEFAULT 0)")
        run("INSERT INTO t (id, name) VALUES (1, 'Alice'), (2, 'Bob')")
        rows = run("SELECT * FROM t")["rows"]
        assert len(rows) == 2
        assert all(r["score"] == 0 for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# Column aliases (AS)
# ─────────────────────────────────────────────────────────────────────────────

class TestColumnAliases:

    def test_select_with_alias(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        run("INSERT INTO t VALUES (1, 'Alice')")
        rows = run("SELECT name AS n FROM t")["rows"]
        assert "n" in rows[0]
        assert rows[0]["n"] == "Alice"

    def test_multiple_aliases(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT, age INT)")
        run("INSERT INTO t VALUES (1, 'Alice', 30)")
        rows = run("SELECT name AS n, age AS a FROM t")["rows"]
        assert rows[0]["n"] == "Alice"
        assert rows[0]["a"] == 30

    def test_alias_with_no_alias_cols(self, db):
        """Mix of aliased and non-aliased columns."""
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, name TEXT, age INT)")
        run("INSERT INTO t VALUES (1, 'Alice', 30)")
        rows = run("SELECT id, name AS n FROM t")["rows"]
        assert "id" in rows[0]
        assert "n" in rows[0]


# ─────────────────────────────────────────────────────────────────────────────
# NOT operator in WHERE
# ─────────────────────────────────────────────────────────────────────────────

class TestNotOperator:

    def test_not_condition(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        run("INSERT INTO t VALUES (1, 20)")
        run("INSERT INTO t VALUES (2, 30)")
        run("INSERT INTO t VALUES (3, 40)")
        rows = run("SELECT * FROM t WHERE NOT age > 25")["rows"]
        assert len(rows) == 1
        assert rows[0]["age"] == 20

    def test_not_with_and(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        run("INSERT INTO t VALUES (1, 20)")
        run("INSERT INTO t VALUES (2, 30)")
        run("INSERT INTO t VALUES (3, 40)")
        rows = run("SELECT * FROM t WHERE NOT age > 25 AND NOT age < 15")["rows"]
        assert len(rows) == 1
        assert rows[0]["age"] == 20


# ─────────────────────────────────────────────────────────────────────────────
# WHERE OR precedence (AND binds tighter than OR)
# ─────────────────────────────────────────────────────────────────────────────

class TestOrPrecedence:

    def test_and_binds_tighter_than_or(self, db):
        """
        'age = 20 OR age = 30 AND id = 2' should be 'age=20 OR (age=30 AND id=2)'.
        So id=1/age=20 matches (via OR), and id=2/age=30 matches (via AND).
        id=3/age=40 does NOT match.
        """
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        run("INSERT INTO t VALUES (1, 20)")
        run("INSERT INTO t VALUES (2, 30)")
        run("INSERT INTO t VALUES (3, 40)")
        rows = run("SELECT * FROM t WHERE age = 20 OR age = 30 AND id = 2")["rows"]
        ids = {r["id"] for r in rows}
        assert ids == {1, 2}

    def test_or_only(self, db):
        _, run = db
        run("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        run("INSERT INTO t VALUES (1, 20)")
        run("INSERT INTO t VALUES (2, 30)")
        run("INSERT INTO t VALUES (3, 40)")
        rows = run("SELECT * FROM t WHERE age = 20 OR age = 40")["rows"]
        ids = {r["id"] for r in rows}
        assert ids == {1, 3}


# ─────────────────────────────────────────────────────────────────────────────
# FULL OUTER JOIN (verifies bug fix for null columns)
# ─────────────────────────────────────────────────────────────────────────────

class TestFullOuterJoin:

    def test_full_join_with_unmatched_rows(self, db):
        """FULL JOIN should include unmatched rows from BOTH sides with proper NULL columns."""
        _, run = db
        run("CREATE TABLE left_t (id INT PRIMARY KEY, val TEXT)")
        run("CREATE TABLE right_t (id INT PRIMARY KEY, val TEXT)")
        run("INSERT INTO left_t VALUES (1, 'a')")
        run("INSERT INTO left_t VALUES (2, 'b')")
        run("INSERT INTO right_t VALUES (2, 'x')")
        run("INSERT INTO right_t VALUES (3, 'y')")
        rows = run(
            "SELECT left_t.id, left_t.val, right_t.id, right_t.val "
            "FROM left_t FULL JOIN right_t ON left_t.id = right_t.id"
        )["rows"]
        # Should have 3 rows: (1,a,NULL,NULL), (2,b,2,x), (NULL,NULL,3,y)
        assert len(rows) == 3
        # Verify unmatched left row has proper NULL column names (not Column repr)
        for row in rows:
            for key in row:
                assert "Column(" not in key, f"Bug: Column object in key: {key}"
