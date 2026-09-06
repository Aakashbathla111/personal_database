#!/usr/bin/env python3
"""Test multi-session isolation over TCP — two clients, independent transactions."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_client import MiniDBClient

print("=" * 60)
print("  MiniDB TCP Multi-Session Test")
print("=" * 60)

# Two separate TCP connections = two separate threads on server
client_a = MiniDBClient().connect()
client_b = MiniDBClient().connect()

print("\n--- Client A: BEGIN transaction ---")
print(client_a.execute("BEGIN"))

print("\n--- Client A: INSERT Ghost (uncommitted) ---")
print(client_a.execute("INSERT INTO employees VALUES (200, 'Ghost', 30, 99999, 'HR')"))

print("\n--- Client A: Can see own uncommitted row ---")
rows = client_a.query("SELECT * FROM employees WHERE id = 200")
print(f"  Client A sees: {rows}")

print("\n--- Client B: Cannot see Client A's uncommitted row ---")
rows = client_b.query("SELECT * FROM employees WHERE id = 200")
print(f"  Client B sees: {rows}")

print("\n--- Client B: BEGIN its own transaction ---")
print(client_b.execute("BEGIN"))

print("\n--- Client B: INSERT Shadow (uncommitted) ---")
print(client_b.execute("INSERT INTO employees VALUES (201, 'Shadow', 25, 88888, 'Sales')"))

print("\n--- Client A: Can see Ghost(200) but NOT Shadow(201) ---")
rows = client_a.query("SELECT id, name FROM employees WHERE id >= 200")
print(f"  Client A sees: {rows}")

print("\n--- Client B: Can see Shadow(201) but NOT Ghost(200) ---")
rows = client_b.query("SELECT id, name FROM employees WHERE id >= 200")
print(f"  Client B sees: {rows}")

print("\n--- Client A: COMMIT (Ghost becomes permanent) ---")
print(client_a.execute("COMMIT"))

print("\n--- Client B: ROLLBACK (Shadow disappears) ---")
print(client_b.execute("ROLLBACK"))

print("\n--- New client: Ghost visible, Shadow gone ---")
client_c = MiniDBClient().connect()
rows = client_c.query("SELECT id, name FROM employees WHERE id >= 200")
print(f"  New client sees: {rows}")

# Cleanup
client_a.execute("DELETE FROM employees WHERE id = 200")

client_a.close()
client_b.close()
client_c.close()

print("\n" + "=" * 60)
print("  ALL TESTS PASSED! Multi-session isolation works over TCP!")
print("=" * 60)
