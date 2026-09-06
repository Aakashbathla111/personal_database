#!/usr/bin/env python3
"""
MiniDB Web UI  —  web_server.py

A browser-based interface for MiniDB with:
  • SQL Query editor
  • DBA Monitor (Claude-powered health checks)
  • Ask in English (AI-powered natural language to SQL)
  • Query Optimizer (AI-powered query analysis)

Usage:
    python web_server.py [--host HOST] [--port PORT]
    # then open http://127.0.0.1:8080 in Chrome
"""

import os
import sys
import uuid
import argparse
import threading

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, Response, make_response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from db_client import MiniDBClient

# Database connection config — points to db_server.py
DB_HOST = os.environ.get("MINIDB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("MINIDB_PORT", "5433"))

app = Flask(__name__)

# ── Connection Pool: persistent TCP connection per browser session ────────
# Each browser gets a cookie (minidb_session) → maps to a persistent
# TCP connection to db_server. This way BEGIN/COMMIT stay on the same
# connection (same thread on db_server = same thread-local transaction).
_pool: dict[str, MiniDBClient] = {}
_pool_lock = threading.Lock()


def _get_client(session_id: str) -> MiniDBClient:
    """Get or create a persistent TCP connection for this browser session."""
    with _pool_lock:
        client = _pool.get(session_id)
        if client and client._sock:
            return client
        client = MiniDBClient(DB_HOST, DB_PORT)
        client.connect()
        _pool[session_id] = client
        return client


def _get_session_id() -> tuple[str, bool]:
    """Read session cookie or generate new one."""
    session_id = request.cookies.get("minidb_session")
    if session_id:
        return session_id, False
    return uuid.uuid4().hex, True


def _make_response(data: dict, new_session: bool, session_id: str):
    """Build JSON response, set cookie if new session."""
    resp = make_response(jsonify(data))
    if new_session:
        resp.set_cookie("minidb_session", session_id,
                        httponly=True, samesite="Lax")
    return resp


print(f"[MiniDB Web] Proxy mode → database at {DB_HOST}:{DB_PORT}")


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MiniDB</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #0f1117;
    color: #e1e4e8;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  header h1 {
    font-size: 20px;
    font-weight: 600;
    color: #58a6ff;
  }
  header h1 span { color: #8b949e; font-weight: 400; }
  .status {
    font-size: 13px;
    color: #3fb950;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .status::before {
    content: '';
    width: 8px; height: 8px;
    background: #3fb950;
    border-radius: 50%;
    display: inline-block;
  }

  /* ── Tabs ── */
  .tab-bar {
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 0 24px;
    display: flex;
    gap: 0;
  }
  .tab-btn {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: #8b949e;
    font-size: 13px;
    font-weight: 500;
    padding: 10px 20px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .tab-btn:hover { color: #e1e4e8; background: none; }
  .tab-btn.active {
    color: #58a6ff;
    border-bottom-color: #58a6ff;
    background: none;
  }
  .tab-panel { display: none; }
  .tab-panel.active { display: flex; flex-direction: column; flex: 1; overflow: hidden; }

  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 16px 24px;
    gap: 12px;
    overflow: hidden;
  }
  .editor-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .editor-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .editor-bar label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #8b949e;
  }
  .btn-group { display: flex; gap: 8px; align-items: center; }
  textarea {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    color: #e1e4e8;
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 14px;
    padding: 12px 16px;
    resize: vertical;
    min-height: 100px;
    outline: none;
    transition: border-color 0.2s;
  }
  textarea:focus { border-color: #58a6ff; }
  button {
    background: #238636;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 8px 20px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
    white-space: nowrap;
  }
  button:hover { background: #2ea043; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.secondary {
    background: transparent;
    border: 1px solid #30363d;
    color: #8b949e;
  }
  button.secondary:hover { color: #e1e4e8; border-color: #8b949e; }
  button.purple { background: #8957e5; }
  button.purple:hover { background: #a371f7; }
  button.orange { background: #d29922; }
  button.orange:hover { background: #e3b341; }
  .results-section {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow: hidden;
  }
  .results-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .results-header label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #8b949e;
  }
  .results-meta {
    font-size: 12px;
    color: #8b949e;
  }
  .results-container {
    flex: 1;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    overflow: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    font-family: 'SF Mono', 'Fira Code', monospace;
  }
  thead th {
    background: #1c2128;
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    color: #58a6ff;
    border-bottom: 1px solid #30363d;
    position: sticky;
    top: 0;
  }
  tbody td {
    padding: 8px 14px;
    border-bottom: 1px solid #21262d;
  }
  tbody tr:hover { background: #1c2128; }
  .message-box {
    padding: 16px;
    font-size: 14px;
    color: #8b949e;
  }
  .message-box.success { color: #3fb950; }
  .message-box.error   { color: #f85149; }
  .message-box.list    { color: #e1e4e8; }
  .message-box.list ul { margin-top: 8px; padding-left: 20px; }
  .message-box.list li { margin-bottom: 4px; }
  .history-section {
    max-height: 120px;
    overflow-y: auto;
  }
  .history-item {
    font-family: 'SF Mono', monospace;
    font-size: 12px;
    padding: 4px 8px;
    color: #8b949e;
    cursor: pointer;
    border-radius: 4px;
  }
  .history-item:hover { background: #1c2128; color: #e1e4e8; }
  .shortcuts {
    font-size: 12px;
    color: #484f58;
    padding: 4px 0;
  }
  kbd {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 11px;
  }

  /* ── Agent report ── */
  .report {
    padding: 20px;
    font-size: 14px;
    line-height: 1.7;
    white-space: pre-wrap;
    font-family: 'SF Mono', 'Fira Code', monospace;
    color: #e1e4e8;
  }
  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: #8b949e;
    cursor: pointer;
  }
  .checkbox-label input { accent-color: #58a6ff; }
  .agent-desc {
    font-size: 13px;
    color: #8b949e;
    line-height: 1.6;
    padding: 12px 0;
  }
  .agent-desc strong { color: #e1e4e8; }
  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid #30363d;
    border-top-color: #58a6ff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-right: 8px;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <h1>MiniDB <span>Web Console</span></h1>
  <div class="status">Connected</div>
</header>

<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('sql')">SQL Query</button>
  <button class="tab-btn" onclick="switchTab('dba')">DBA Monitor</button>
  <button class="tab-btn" onclick="switchTab('ask')">Ask in English</button>
  <button class="tab-btn" onclick="switchTab('optimizer')">Query Optimizer</button>
</div>

<!-- ════════ SQL Query Tab ════════ -->
<div id="tab-sql" class="tab-panel active">
  <div class="main">
    <div class="editor-section">
      <div class="editor-bar">
        <label>SQL Query</label>
        <div class="btn-group">
          <button class="secondary" onclick="clearEditor()">Clear</button>
          <button onclick="runQuery()">Run Query</button>
        </div>
      </div>
      <textarea id="sql" placeholder="Enter SQL here... (e.g. SHOW TABLES)" spellcheck="false">SHOW TABLES</textarea>
      <div class="shortcuts"><kbd>Ctrl</kbd>+<kbd>Enter</kbd> to run &nbsp; | &nbsp; <kbd>Ctrl</kbd>+<kbd>L</kbd> to clear</div>
    </div>
    <div class="results-section">
      <div class="results-header">
        <label>Results</label>
        <span class="results-meta" id="meta"></span>
      </div>
      <div class="results-container" id="results">
        <div class="message-box">Run a query to see results.</div>
      </div>
    </div>
    <div class="history-section" id="history"></div>
  </div>
</div>

<!-- ════════ DBA Monitor Tab ════════ -->
<div id="tab-dba" class="tab-panel">
  <div class="main">
    <div class="editor-section">
      <div class="editor-bar">
        <label>DBA Monitor</label>
        <div class="btn-group">
          <label class="checkbox-label">
            <input type="checkbox" id="dba-autofix"> Auto-fix (create missing indexes)
          </label>
          <button class="purple" id="dba-run-btn" onclick="runDBA()">Run Health Check</button>
        </div>
      </div>
      <div class="agent-desc">
        <strong>Claude-powered database health monitor.</strong> Inspects table stats, WAL health,
        index coverage, and produces a structured report with issues and recommendations.
        Enable <em>Auto-fix</em> to let it create missing indexes automatically.
      </div>
    </div>
    <div class="results-section">
      <div class="results-header">
        <label>Health Report</label>
        <span class="results-meta" id="dba-meta"></span>
      </div>
      <div class="results-container" id="dba-results">
        <div class="message-box">Click "Run Health Check" to start the DBA agent.</div>
      </div>
    </div>
  </div>
</div>

<!-- ════════ Ask in English Tab ════════ -->
<div id="tab-ask" class="tab-panel">
  <div class="main">
    <div class="editor-section">
      <div class="editor-bar">
        <label>Ask in English</label>
        <div class="btn-group">
          <button class="orange" id="ask-run-btn" onclick="runAsk()">Ask</button>
        </div>
      </div>
      <textarea id="ask-input" placeholder='Ask a question in plain English... (e.g. "show me all users older than 25")' spellcheck="false"></textarea>
      <div class="agent-desc">
        <strong>AI-powered natural language to SQL.</strong> Type your question in plain English
        and the AI will generate and run the SQL query for you.
      </div>
    </div>
    <div class="results-section">
      <div class="results-header">
        <label>Results</label>
        <span class="results-meta" id="ask-meta"></span>
      </div>
      <div class="results-container" id="ask-results">
        <div class="message-box">Type a question and click "Ask" to get started.</div>
      </div>
    </div>
  </div>
</div>

<!-- ════════ Query Optimizer Tab ════════ -->
<div id="tab-optimizer" class="tab-panel">
  <div class="main">
    <div class="editor-section">
      <div class="editor-bar">
        <label>Query Optimizer</label>
        <div class="btn-group">
          <label class="checkbox-label">
            <input type="checkbox" id="opt-autofix"> Auto-fix (create indexes)
          </label>
          <button class="orange" id="opt-run-btn" onclick="runOptimizer()">Optimize Query</button>
        </div>
      </div>
      <textarea id="opt-sql" placeholder="Enter a SELECT query to optimize..." spellcheck="false"></textarea>
      <div class="agent-desc">
        <strong>AI-powered query optimizer.</strong> Analyses the execution plan, benchmarks
        performance, identifies missing indexes, suggests query rewrites, and produces a
        before/after comparison report.
      </div>
    </div>
    <div class="results-section">
      <div class="results-header">
        <label>Optimization Report</label>
        <span class="results-meta" id="opt-meta"></span>
      </div>
      <div class="results-container" id="opt-results">
        <div class="message-box">Enter a SELECT query and click "Optimize Query" to start.</div>
      </div>
    </div>
  </div>
</div>

<script>
/* ── Tab switching ── */
function switchTab(name) {
  const map = { sql: 'sql', dba: 'dba', ask: 'ask', optimizer: 'optimizer' };
  document.querySelectorAll('.tab-btn').forEach(b => {
    const txt = b.textContent.toLowerCase();
    const match = (name === 'sql' && txt.includes('sql query'))
               || (name === 'dba' && txt.includes('dba'))
               || (name === 'ask' && txt.includes('ask'))
               || (name === 'optimizer' && txt.includes('optimizer'));
    b.classList.toggle('active', match);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
}

/* ── SQL Query Tab ── */
const sqlEl      = document.getElementById('sql');
const resultsEl  = document.getElementById('results');
const metaEl     = document.getElementById('meta');
const historyEl  = document.getElementById('history');
let history = [];

sqlEl.addEventListener('keydown', e => {
  if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); runQuery(); }
  if (e.ctrlKey && e.key === 'l')     { e.preventDefault(); clearEditor(); }
});

function clearEditor() { sqlEl.value = ''; sqlEl.focus(); }

async function runQuery() {
  const sql = sqlEl.value.trim();
  if (!sql) return;

  resultsEl.innerHTML = '<div class="message-box">Running...</div>';
  metaEl.textContent = '';

  const t0 = performance.now();
  try {
    const res  = await fetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql })
    });
    const data = await res.json();
    const ms   = (performance.now() - t0).toFixed(1);

    if (!data.ok) {
      resultsEl.innerHTML = `<div class="message-box error">ERROR: ${esc(data.error)}</div>`;
      metaEl.textContent = `${ms} ms`;
      return;
    }

    let html = '';

    if (data.rows && data.rows.length > 0) {
      const cols = Object.keys(data.rows[0]);
      html += '<table><thead><tr>' + cols.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead><tbody>';
      for (const row of data.rows) {
        html += '<tr>' + cols.map(c => `<td>${esc(String(row[c] ?? 'NULL'))}</td>`).join('') + '</tr>';
      }
      html += '</tbody></table>';
      metaEl.textContent = `${data.rows.length} row(s) \u2022 ${ms} ms`;
    } else if (data.rows && data.rows.length === 0) {
      html += '<div class="message-box">(no rows)</div>';
      metaEl.textContent = ms + ' ms';
    }

    if (data.message) {
      html += `<div class="message-box success">${esc(data.message)}</div>`;
      if (!data.rows) metaEl.textContent = ms + ' ms';
    }

    if (data.list && data.list.length > 0) {
      html += '<div class="message-box list"><ul>' + data.list.map(i => `<li>${esc(i)}</li>`).join('') + '</ul></div>';
      if (!data.rows) metaEl.textContent = `${data.list.length} item(s) \u2022 ${ms} ms`;
    }

    if (!html) html = '<div class="message-box success">OK</div>';
    resultsEl.innerHTML = html;

    if (!history.includes(sql)) {
      history.unshift(sql);
      if (history.length > 20) history.pop();
      renderHistory();
    }

  } catch (err) {
    resultsEl.innerHTML = `<div class="message-box error">Network error: ${esc(err.message)}</div>`;
  }
}

function renderHistory() {
  historyEl.innerHTML = history.map(s =>
    `<div class="history-item" onclick="sqlEl.value=this.textContent;sqlEl.focus()">${esc(s)}</div>`
  ).join('');
}

/* ── DBA Monitor Tab ── */
async function runDBA() {
  const btn = document.getElementById('dba-run-btn');
  const res = document.getElementById('dba-results');
  const meta = document.getElementById('dba-meta');
  const autofix = document.getElementById('dba-autofix').checked;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Running...';
  res.innerHTML = '<div class="message-box"><span class="spinner"></span>Claude is analysing your database... This may take 15-30 seconds.</div>';
  meta.textContent = '';

  const t0 = performance.now();
  try {
    const resp = await fetch('/dba-monitor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_fix: autofix })
    });
    const data = await resp.json();
    const ms = ((performance.now() - t0) / 1000).toFixed(1);

    if (!data.ok) {
      res.innerHTML = `<div class="message-box error">ERROR: ${esc(data.error)}</div>`;
    } else {
      res.innerHTML = `<div class="report">${esc(data.report)}</div>`;
    }
    meta.textContent = `${ms}s`;
  } catch (err) {
    res.innerHTML = `<div class="message-box error">Network error: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run Health Check';
  }
}

/* ── Ask in English Tab ── */
async function runAsk() {
  const btn = document.getElementById('ask-run-btn');
  const res = document.getElementById('ask-results');
  const meta = document.getElementById('ask-meta');
  const question = document.getElementById('ask-input').value.trim();

  if (!question) {
    res.innerHTML = '<div class="message-box error">Please enter a question.</div>';
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Thinking...';
  res.innerHTML = '<div class="message-box"><span class="spinner"></span>Generating SQL from your question...</div>';
  meta.textContent = '';

  const t0 = performance.now();
  try {
    const resp = await fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question })
    });
    const data = await resp.json();
    const ms = ((performance.now() - t0) / 1000).toFixed(1);

    if (!data.ok) {
      res.innerHTML = `<div class="message-box error">ERROR: ${esc(data.error)}</div>`;
      meta.textContent = `${ms}s`;
      return;
    }

    let html = '';

    if (data.sql) {
      html += `<div class="message-box" style="color:#d29922;font-family:monospace;border-bottom:1px solid #30363d;">Generated SQL: ${esc(data.sql)}</div>`;
    }

    if (data.rows && data.rows.length > 0) {
      const cols = Object.keys(data.rows[0]);
      html += '<table><thead><tr>' + cols.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead><tbody>';
      for (const row of data.rows) {
        html += '<tr>' + cols.map(c => `<td>${esc(String(row[c] ?? 'NULL'))}</td>`).join('') + '</tr>';
      }
      html += '</tbody></table>';
      meta.textContent = `${data.rows.length} row(s) • ${ms}s`;
    } else if (data.message) {
      html += `<div class="message-box success">${esc(data.message)}</div>`;
      meta.textContent = `${ms}s`;
    } else if (data.list && data.list.length > 0) {
      html += '<div class="message-box list"><ul>' + data.list.map(i => `<li>${esc(i)}</li>`).join('') + '</ul></div>';
      meta.textContent = `${data.list.length} item(s) • ${ms}s`;
    } else {
      html += '<div class="message-box">(no results)</div>';
      meta.textContent = `${ms}s`;
    }

    res.innerHTML = html;
  } catch (err) {
    res.innerHTML = `<div class="message-box error">Network error: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Ask';
  }
}

/* ── Query Optimizer Tab ── */
async function runOptimizer() {
  const btn = document.getElementById('opt-run-btn');
  const res = document.getElementById('opt-results');
  const meta = document.getElementById('opt-meta');
  const sql = document.getElementById('opt-sql').value.trim();
  const autofix = document.getElementById('opt-autofix').checked;

  if (!sql) {
    res.innerHTML = '<div class="message-box error">Please enter a SELECT query to optimize.</div>';
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Optimizing...';
  res.innerHTML = '<div class="message-box"><span class="spinner"></span>AI is analysing your query... This may take 15-30 seconds.</div>';
  meta.textContent = '';

  const t0 = performance.now();
  try {
    const resp = await fetch('/query-optimizer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql, auto_fix: autofix })
    });
    const data = await resp.json();
    const ms = ((performance.now() - t0) / 1000).toFixed(1);

    if (!data.ok) {
      res.innerHTML = `<div class="message-box error">ERROR: ${esc(data.error)}</div>`;
    } else {
      res.innerHTML = `<div class="report">${esc(data.report)}</div>`;
    }
    meta.textContent = `${ms}s`;
  } catch (err) {
    res.innerHTML = `<div class="message-box error">Network error: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Optimize Query';
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(HTML_PAGE, content_type="text/html")


@app.route("/query", methods=["POST"])
def query():
    body = request.get_json(force=True)
    sql  = (body.get("sql") or "").strip().rstrip(";").strip()
    if not sql:
        return jsonify({"ok": False, "error": "Empty query"})

    session_id, new_session = _get_session_id()

    try:
        client = _get_client(session_id)
        result = client.raw(sql)
        return _make_response(result, new_session, session_id)

    except (ConnectionRefusedError, ConnectionError):
        # Connection dead — remove from pool and retry once
        with _pool_lock:
            _pool.pop(session_id, None)
        try:
            client = _get_client(session_id)
            result = client.raw(sql)
            return _make_response(result, new_session, session_id)
        except Exception:
            return jsonify({"ok": False, "error": "Database server not running. Start db_server.py first."})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected: {e}"})


@app.route("/dba-monitor", methods=["POST"])
def dba_monitor():
    body     = request.get_json(force=True)
    auto_fix = body.get("auto_fix", False)
    try:
        with MiniDBClient(DB_HOST, DB_PORT) as db:
            result = db.send_cmd({"cmd": "dba-monitor", "auto_fix": auto_fix})
        return jsonify(result)
    except ConnectionRefusedError:
        return jsonify({"ok": False, "error": "Database server not running."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/ask", methods=["POST"])
def ask():
    body     = request.get_json(force=True)
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "Empty question"})
    try:
        with MiniDBClient(DB_HOST, DB_PORT) as db:
            result = db.send_cmd({"cmd": "ask", "question": question})
        return jsonify(result)
    except ConnectionRefusedError:
        return jsonify({"ok": False, "error": "Database server not running."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/query-optimizer", methods=["POST"])
def query_optimizer():
    body     = request.get_json(force=True)
    sql      = (body.get("sql") or "").strip().rstrip(";").strip()
    auto_fix = body.get("auto_fix", False)
    if not sql:
        return jsonify({"ok": False, "error": "Empty query"})
    try:
        with MiniDBClient(DB_HOST, DB_PORT) as db:
            result = db.send_cmd({"cmd": "query-optimizer", "sql": sql, "auto_fix": auto_fix})
        return jsonify(result)
    except ConnectionRefusedError:
        return jsonify({"ok": False, "error": "Database server not running."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MiniDB Web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default=8080, type=int)
    args = ap.parse_args()

    print(f"[MiniDB Web] Open http://{args.host}:{args.port} in Chrome")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
