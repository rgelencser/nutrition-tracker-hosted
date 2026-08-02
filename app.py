"""
NutriTool (hosted) backend -- Flask + SQLite, meant to run on PythonAnywhere.

This is the always-reachable companion to nutritool-local: same frontend,
same nutrient/decay model, but data lives in a SQLite database on the
server instead of a JSON file on your own machine, and the whole thing sits
behind a single shared password since the URL is public. See README.md for
why this trade-off (internet + login, in exchange for working from any
device including mobile) exists alongside the local version.

Routes:
    GET  /              -- the frontend (login-gated)
    GET  /health         -- unauthenticated liveness check for deploy.py
    GET  /login          -- password form
    POST /login           -- checks the password, starts a session
    GET  /logout          -- clears the session
    GET  /api/data        -- login-gated: returns {config, customFoods, log}
    POST /api/data         -- login-gated: full or partial update to that dataset

Storage: plain sqlite3 (stdlib), not SQLAlchemy -- three small tables
(config, custom_foods, log_entries) are simple enough that an ORM would
just be extra dependency weight for no real benefit here. Nutrient payloads
are stored as JSON text columns so the on-disk shape matches the JSON the
frontend already sends/receives, keeping this file (and the frontend) a
close cousin of nutritool-local's app.py rather than a rewrite.

Secrets (the shared password, Flask's session-signing secret key) are never
hardcoded. They're read from environment variables -- on PythonAnywhere, set
these in the Web tab's "Environment variables" section; locally, either
export them in your shell or drop them in a gitignored .env file (a tiny
stdlib-only loader below reads that file if present; no python-dotenv
dependency needed for something this small).
"""

import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session, url_for

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NUTRITOOL_DB_PATH", str(APP_DIR / "nutritool.db")))


def load_dotenv_if_present():
    """Tiny stdlib-only .env loader -- doesn't override already-set env vars."""
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv_if_present()

APP_PASSWORD = os.environ.get("NUTRITOOL_PASSWORD")
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print(
        "WARNING: SECRET_KEY is not set -- using a random one-time key, so everyone's "
        "session will be invalidated on every restart/reload. Set a fixed SECRET_KEY "
        "environment variable to avoid that (see README)."
    )
if not APP_PASSWORD:
    print(
        "WARNING: NUTRITOOL_PASSWORD is not set -- the login page will refuse every "
        "password until you set it (fails closed, not open). See README."
    )

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

EMPTY_DATASET = {"config": {}, "customFoods": [], "log": []}


# ============================================================================
# DATABASE
# ============================================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(get_db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS custom_foods (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                nutrients TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS log_entries (
                id TEXT PRIMARY KEY,
                food_id TEXT,
                food_name TEXT NOT NULL,
                grams REAL NOT NULL,
                timestamp INTEGER NOT NULL,
                per100g TEXT NOT NULL,
                nutrients TEXT NOT NULL
            );
            """
        )
        conn.commit()


def read_dataset():
    with closing(get_db()) as conn:
        config_row = conn.execute("SELECT data FROM config WHERE id = 1").fetchone()
        config = json.loads(config_row["data"]) if config_row else {}

        custom_foods = [
            {"id": r["id"], "name": r["name"], "source": "custom", "nutrients": json.loads(r["nutrients"])}
            for r in conn.execute("SELECT id, name, nutrients FROM custom_foods").fetchall()
        ]

        log = [
            {
                "id": r["id"],
                "foodId": r["food_id"],
                "foodName": r["food_name"],
                "grams": r["grams"],
                "timestamp": r["timestamp"],
                "per100g": json.loads(r["per100g"]),
                "nutrients": json.loads(r["nutrients"]),
            }
            for r in conn.execute("SELECT * FROM log_entries ORDER BY timestamp").fetchall()
        ]

    return {"config": config, "customFoods": custom_foods, "log": log}


def write_config(config):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO config (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (json.dumps(config),),
        )
        conn.commit()


def write_custom_foods(custom_foods):
    # Whole-collection replace, same "small data, whole-file-ish write is
    # fine" simplicity as nutritool-local -- no per-item add/remove endpoints.
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM custom_foods")
        conn.executemany(
            "INSERT INTO custom_foods (id, name, nutrients) VALUES (?, ?, ?)",
            [(f["id"], f["name"], json.dumps(f.get("nutrients", {}))) for f in custom_foods],
        )
        conn.commit()


def write_log(log):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM log_entries")
        conn.executemany(
            "INSERT INTO log_entries (id, food_id, food_name, grams, timestamp, per100g, nutrients) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e["id"],
                    e.get("foodId"),
                    e["foodName"],
                    e["grams"],
                    e["timestamp"],
                    json.dumps(e.get("per100g", {})),
                    json.dumps(e.get("nutrients", {})),
                )
                for e in log
            ],
        )
        conn.commit()


init_db()


# ============================================================================
# AUTH -- single shared password, Flask session cookie. No multi-user
# accounts: this is still a small single-user tool, just reachable from
# anywhere, so the only thing guarding it is one password.
# ============================================================================
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>NutriTool -- Sign in</title>
<style>
  :root {{ --bg:#f5f6f8; --panel:#fff; --border:#e0e2e7; --text:#1c1e21; --text-dim:#6b7280; --accent:#2563eb; --bad:#dc2626; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; background:var(--bg); color:var(--text);
  }}
  form {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:28px; width:280px; }}
  h1 {{ font-size:18px; margin:0 0 4px 0; }}
  p.sub {{ color:var(--text-dim); font-size:12px; margin:0 0 16px 0; }}
  label {{ display:block; font-size:12px; color:var(--text-dim); margin-bottom:4px; }}
  input {{ width:100%; padding:8px; border:1px solid var(--border); border-radius:5px; font-size:14px; }}
  button {{
    margin-top:14px; width:100%; padding:8px; border:none; border-radius:5px;
    background:var(--accent); color:#fff; font-weight:600; font-size:14px; cursor:pointer;
  }}
  .error {{ color:var(--bad); font-size:12.5px; margin-top:10px; }}
</style>
</head>
<body>
<form method="post">
  <h1>NutriTool</h1>
  <p class="sub">Enter the shared password to continue.</p>
  <label>Password</label>
  <input type="password" name="password" autofocus />
  <button type="submit">Sign in</button>
  {error_html}
</form>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error_html = ""
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if APP_PASSWORD and secrets.compare_digest(submitted, APP_PASSWORD):
            session.clear()
            session["authed"] = True
            session.permanent = True
            next_path = request.args.get("next")
            return redirect(next_path if next_path else url_for("index"))
        error_html = '<div class="error">Incorrect password.</div>'
    return LOGIN_PAGE.format(error_html=error_html)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================================
# APP ROUTES
# ============================================================================
@app.route("/")
@login_required
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    # Deliberately unauthenticated and content-free (see README for why this
    # was chosen over the login page as deploy.py's health-check target).
    return "ok", 200


@app.route("/api/data", methods=["GET"])
@login_required
def get_data():
    return jsonify(read_dataset())


@app.route("/api/data", methods=["POST"])
@login_required
def post_data():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    if "config" in payload:
        write_config(payload["config"])
    if "customFoods" in payload:
        write_custom_foods(payload["customFoods"])
    if "log" in payload:
        write_log(payload["log"])

    return jsonify(read_dataset())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Using database: {DB_PATH}")
    print(f"\nNutriTool (hosted, local test mode) running at: http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
