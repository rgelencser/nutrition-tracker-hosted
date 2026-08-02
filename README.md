# NutriTool (hosted)

The always-reachable companion to [nutritool-local](https://github.com/rgelencser/nutrition-tracker):
same nutrient panel, personalization, decay/lookback model, and food
database, but data lives in a hosted SQLite database instead of a local
JSON file, behind a single shared password, on PythonAnywhere.

## Why this exists alongside nutritool-local

nutritool-local runs entirely offline on your own machine — no login, no
internet needed, but you have to be at a device running its server to log
food or see your dashboard. This version trades that for the opposite: you
need internet and a password every session, but you can log food from your
phone the moment you eat it, or from any browser on any device, because the
app and its data live on a public server rather than your laptop.

Neither replaces the other; they're separate, independent repos with
separate data. Pick whichever fits the moment — this repo is for "I'm out
and just ate something," nutritool-local is for "I'm at my desk and don't
want an account."

## Architecture

- **Frontend**: identical nutrient panel, RDA/target logic, decay/lookback
  model, food database, and UI to nutritool-local (`static/index.html`) —
  copied over unchanged except for the persistence layer's comments and a
  couple of lines of copy that describe "hosted" instead of "local file."
  See nutritool-local's README for the full nutrient/decay model writeup;
  it applies here verbatim.
- **Backend**: Flask (`app.py`), same `/api/data` GET/POST contract as
  nutritool-local, but backed by SQLite (plain `sqlite3`, not an ORM —
  three small tables for a single-user tool didn't justify adding
  SQLAlchemy as a dependency) instead of a JSON file.
- **Auth**: a single shared password gates the whole app via a signed
  Flask session cookie (`NUTRITOOL_PASSWORD` env var) — no multi-user
  accounts, this is still a personal tool, just a publicly-reachable one.
- **HTTPS**: provided automatically by PythonAnywhere's `*.pythonanywhere.com`
  subdomain. Nothing in this app configures TLS itself — just confirm the
  padlock is present on your live URL after deploying.

## Local testing (before you deploy)

```bash
pip install -r requirements.txt
NUTRITOOL_PASSWORD=choose-something SECRET_KEY=$(python -c "import secrets;print(secrets.token_hex(32))") python app.py
```

Open `http://localhost:5000`, which will redirect you to `/login`. This is
for testing the app itself before pushing it live — it does not need to be
internet-reachable to test locally.

## Password & secret key setup

Two environment variables are required; neither is ever hardcoded or
committed:

| Variable | Purpose |
|---|---|
| `NUTRITOOL_PASSWORD` | the one shared password the login page checks against |
| `SECRET_KEY` | signs the session cookie; without it, a random one is generated at every process start, which logs everyone out on every reload |

**On PythonAnywhere**: set both in the **Web** tab's *Environment variables*
section (Account → Web → your app → Environment variables), then Reload.

**Locally**: export them in your shell, or create a `.env` file next to
`app.py` (gitignored, never committed):

```
NUTRITOOL_PASSWORD=choose-something-only-you-know
SECRET_KEY=some-long-random-string
```

If `NUTRITOOL_PASSWORD` isn't set, the login page will refuse every
password — it fails closed, never open.

## Deploying to PythonAnywhere

PythonAnywhere's **free tier has no SSH access** (that's a paid-only
feature), so there are two ways to get a new commit live: a scripted path
and a manual fallback. Both assume you've already done the one-time setup
below.

### One-time setup

1. Create a PythonAnywhere account and, from a **Bash console** in their
   dashboard, clone this repo: `git clone https://github.com/rgelencser/nutrition-tracker-hosted.git`
2. Create a new web app (**Web** tab → Add a new web app → Manual
   configuration → your Python version).
3. Point its **WSGI configuration file** at this app — replace its
   generated content with something like:
   ```python
   import sys
   path = '/home/yourusername/nutrition-tracker-hosted'
   if path not in sys.path:
       sys.path.insert(0, path)
   from app import app as application
   ```
4. Set the **Source code** / **Working directory** to the cloned repo path.
5. Install dependencies in a virtualenv PythonAnywhere's Web tab points at:
   `pip install -r requirements.txt` (from a Bash console, inside that
   virtualenv).
6. Set `NUTRITOOL_PASSWORD` and `SECRET_KEY` in the Web tab's environment
   variables (see above), then hit **Reload**.
7. Confirm HTTPS is active on your `*.pythonanywhere.com` URL (it should be
   automatic — just check for the padlock).

### Path 1 — scripted (`deploy.py`)

```bash
python deploy.py
```

This runs, in order, failing loudly and distinctly at whichever stage
breaks rather than silently reporting success:

1. `git push origin main` (local)
2. Opens a PythonAnywhere bash console via their API — reusing one from a
   previous deploy if it's still alive, to avoid piling up idle consoles
3. Sends `git pull origin main` to that console and watches its output
   for a marker this script appends (`echo DEPLOY_MARKER_$?`) to reliably
   detect the pull's real exit code, since the console API is just a
   text stream, not a structured command-result API
4. Calls PythonAnywhere's reload API for your web app
5. **Health check**: `GET https://yourdomain.pythonanywhere.com/health`,
   retried a few times, must return `200` with body `ok`

**Why `/health` and not the login page**, as the thing this script checks:
a dedicated route returns the same fixed, tiny, unauthenticated response
regardless of template/markup changes to the login page over time, so the
check can't produce a false negative just because someone redesigned the
login form. It also doesn't need the script to juggle session cookies or
worry about accidentally probing something that looks like a real login
attempt.

Required environment variables for `deploy.py` (same `.env`-file-or-shell-export
pattern as above; add these to the same `.env` file if you're using one):

| Variable | Purpose |
|---|---|
| `PYTHONANYWHERE_API_TOKEN` | Account → API Token on PythonAnywhere |
| `PYTHONANYWHERE_USERNAME` | your PythonAnywhere username |
| `PYTHONANYWHERE_DOMAIN` | e.g. `yourusername.pythonanywhere.com` |
| `PYTHONANYWHERE_REPO_PATH` | absolute path to the clone on PA, e.g. `/home/yourusername/nutrition-tracker-hosted` |

**This depends on an unofficial API.** PythonAnywhere's own staff describe
the console-automation API as unofficial/beta — it can change shape or
break without notice. If `deploy.py` starts failing for reasons that look
like the API itself changed (not a real deploy problem), check
https://www.pythonanywhere.com/api/v0/ against what this script sends, or
just use the manual fallback below while you fix it.

### Path 2 — manual fallback (always works, not just a last resort)

1. PythonAnywhere dashboard → open a **Bash console**
2. `cd nutrition-tracker-hosted && git pull origin main`
3. **Web** tab → **Reload**

That's it — three steps, no API involved. Use this any time the script
doesn't work on your setup, or if you'd just rather not depend on an
unofficial API for something this occasional.

## Data storage

SQLite, one file (`nutritool.db` by default, next to `app.py` — override
with the `NUTRITOOL_DB_PATH` env var if you want it elsewhere). Three
tables: `config` (one row, your personalization settings as JSON),
`custom_foods`, and `log_entries` — nutrient payloads are stored as JSON
text columns so the on-disk shape mirrors the `/api/data` JSON the frontend
already sends, keeping this close to nutritool-local's data model rather
than a from-scratch schema.

No accounts beyond the single shared password — this remains a personal,
single-user tool, just one that happens to be reachable from anywhere.

## What's carried over unchanged from nutritool-local

The 29-nutrient panel, personalization/RDA logic (Mifflin-St Jeor calories,
NIH ODS / DRI targets), the three-class decay model (daily-reset, the
calcium no-decay exception, and the exponential lookback windows including
choline's explicit 7-day override), the ~100-food seed database, and the
dashboard/UI layout are all identical to nutritool-local — see that
project's README for the full nutrient table, decay/lookback table with
sourced-vs-estimated citations, and data sourcing notes. Only the storage
and access-control layers differ here.

## Non-goals

No multi-user accounts, no fine-grained permissions, no built-in backup/
export tooling beyond what SQLite itself gives you, no attempt at clinical
authority (same disclaimer as nutritool-local: this is a personal-tracking
heuristic, not medical advice).
