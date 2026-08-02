#!/usr/bin/env python3
"""
One-command redeploy for NutriTool (hosted) on PythonAnywhere.

PythonAnywhere's free tier has no SSH access, so this drives their web-based
"consoles" API to run `git pull` for us instead. That API is explicitly
unofficial/in-beta per PythonAnywhere's own staff -- it can change without
notice. If this script starts failing, check PythonAnywhere's current API
docs (https://www.pythonanywhere.com/api/v0/) against the endpoints used
below, or just do the manual fallback documented in README.md.

Pipeline (each stage fails loudly and distinctly -- this script never
reports success unless every stage actually succeeded):
  1. git push origin main                         (local)
  2. open or reuse a PythonAnywhere bash console    (API)
  3. send `git pull origin main` to that console     (API)
  4. reload the web app                               (API)
  5. HTTP GET /health on the live site and confirm 200 (real request)

Required environment variables (set in your shell, or in a gitignored
.env file next to this script -- see README.md "Deploy script setup"):
  PYTHONANYWHERE_API_TOKEN     your API token (Account -> API Token)
  PYTHONANYWHERE_USERNAME      your PythonAnywhere username
  PYTHONANYWHERE_DOMAIN        e.g. yourusername.pythonanywhere.com
  PYTHONANYWHERE_REPO_PATH     absolute path to the repo clone on PA,
                                e.g. /home/yourusername/nutrition-tracker-hosted
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = APP_DIR / ".deploy_state.json"


def load_dotenv_if_present():
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv_if_present()

API_TOKEN = os.environ.get("PYTHONANYWHERE_API_TOKEN")
USERNAME = os.environ.get("PYTHONANYWHERE_USERNAME")
DOMAIN = os.environ.get("PYTHONANYWHERE_DOMAIN")
REPO_PATH = os.environ.get("PYTHONANYWHERE_REPO_PATH")

API_BASE = "https://www.pythonanywhere.com/api/v0"
CONSOLE_POLL_INTERVAL_SEC = 1.5
CONSOLE_POLL_TIMEOUT_SEC = 60
HEALTH_CHECK_RETRIES = 6
HEALTH_CHECK_RETRY_DELAY_SEC = 2
MARKER = "DEPLOY_MARKER_"


def fail(stage, message, detail=None):
    print(f"\n[{stage} FAILED] {message}")
    if detail is not None:
        print("---- exact response/output, for diagnosing against PythonAnywhere's current API docs ----")
        print(detail)
        print("---------------------------------------------------------------------------------------")
    print(
        "\nReminder: the PythonAnywhere consoles API this script drives is unofficial/beta and can "
        "change without notice. If this looks like an API shape change rather than a real problem, "
        "check https://www.pythonanywhere.com/api/v0/ (or their forum) -- or just do the manual "
        "fallback in README.md: dashboard -> Bash console -> git pull -> Web tab -> Reload."
    )
    sys.exit(1)


def require_config():
    missing = [
        name
        for name, val in [
            ("PYTHONANYWHERE_API_TOKEN", API_TOKEN),
            ("PYTHONANYWHERE_USERNAME", USERNAME),
            ("PYTHONANYWHERE_DOMAIN", DOMAIN),
            ("PYTHONANYWHERE_REPO_PATH", REPO_PATH),
        ]
        if not val
    ]
    if missing:
        fail(
            "CONFIG",
            "Missing required environment variable(s): " + ", ".join(missing) + ". "
            "Set them in your shell or in a gitignored .env file next to deploy.py -- see README.md.",
        )


def api_request(method, path, body=None):
    url = f"{API_BASE}/user/{USERNAME}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Token {API_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, raw
    except urllib.error.URLError as e:
        return None, str(e.reason)


def step_git_push():
    print("[1/5] Pushing local commits to GitHub...")
    result = subprocess.run(
        ["git", "push", "origin", "main"], cwd=APP_DIR, capture_output=True, text=True
    )
    if result.returncode != 0:
        fail("GIT PUSH", "`git push origin main` failed.", result.stderr or result.stdout)
    print("      Pushed.")


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def step_get_console():
    print("[2/5] Opening a PythonAnywhere console (reusing one if we already have a live one)...")
    state = load_state()
    console_id = state.get("console_id")

    if console_id is not None:
        status, _ = api_request("GET", f"/consoles/{console_id}/")
        if status == 200:
            print(f"      Reusing existing console #{console_id}.")
            return console_id
        # Stale/closed -- fall through and create a new one.

    status, body = api_request("POST", "/consoles/", {"executable": "bash", "arguments": ""})
    if status != 200:
        fail("OPEN CONSOLE", f"Could not create a console (HTTP {status}).", body)
    try:
        console_id = json.loads(body)["id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        fail("OPEN CONSOLE", "Console creation returned an unexpected response shape.", body)

    save_state({"console_id": console_id})
    print(f"      Created console #{console_id}.")
    return console_id


def step_git_pull(console_id):
    print("[3/5] Sending `git pull origin main` to the console...")
    command = f"cd {REPO_PATH} && git pull origin main; echo {MARKER}$?\n"
    status, body = api_request("POST", f"/consoles/{console_id}/send_input/", {"input": command})
    if status != 200:
        fail("GIT PULL", f"Could not send input to the console (HTTP {status}).", body)

    deadline = time.time() + CONSOLE_POLL_TIMEOUT_SEC
    last_output = ""
    while time.time() < deadline:
        time.sleep(CONSOLE_POLL_INTERVAL_SEC)
        status, body = api_request("GET", f"/consoles/{console_id}/get_latest_output/")
        if status != 200:
            fail("GIT PULL", f"Could not read console output (HTTP {status}).", body)
        try:
            last_output = json.loads(body).get("output", "")
        except json.JSONDecodeError:
            last_output = body

        if MARKER in last_output:
            exit_code_str = last_output.split(MARKER, 1)[1].strip().split()[0]
            try:
                exit_code = int(exit_code_str)
            except ValueError:
                fail("GIT PULL", "Couldn't parse the git pull exit code from console output.", last_output)
            if exit_code != 0:
                fail("GIT PULL", f"`git pull origin main` exited {exit_code} on PythonAnywhere.", last_output)
            print("      Pulled successfully.")
            return

    fail("GIT PULL", f"Timed out after {CONSOLE_POLL_TIMEOUT_SEC}s waiting for the console to finish.", last_output)


def step_reload():
    print("[4/5] Reloading the web app...")
    status, body = api_request("POST", f"/webapps/{DOMAIN}/reload/")
    if status != 200:
        fail("RELOAD", f"Reload request failed (HTTP {status}).", body)
    print("      Reload requested.")


def step_health_check():
    print("[5/5] Health-checking the live site...")
    url = f"https://{DOMAIN}/health"
    last_error = None
    for attempt in range(1, HEALTH_CHECK_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
                if resp.status == 200 and body == "ok":
                    print(f"      {url} -> 200 \"ok\". Deploy succeeded.")
                    return
                last_error = f"HTTP {resp.status}, body: {body!r}"
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_error = str(e)
        if attempt < HEALTH_CHECK_RETRIES:
            time.sleep(HEALTH_CHECK_RETRY_DELAY_SEC)

    fail("HEALTH CHECK", f"{url} never returned the expected 200 \"ok\" after reload.", last_error)


def main():
    require_config()
    step_git_push()
    console_id = step_get_console()
    step_git_pull(console_id)
    step_reload()
    step_health_check()


if __name__ == "__main__":
    main()
