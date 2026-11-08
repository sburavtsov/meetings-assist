#!/usr/bin/env python3
"""One-time helper: create a Yougile REST API key and store it in ~/.meetings-assist/secrets.json.

Run it yourself in a terminal:   python3 tools/yougile_setup_key.py
It asks for your Yougile login and password (password is not echoed), creates a key for the company set in
config.json (tasks.adapters.yougile.company_id) and saves it. The password is sent only to Yougile and never saved.
"""
import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import secrets_config  # noqa: E402
import settings  # noqa: E402

cfg = settings.S["tasks"]["adapters"]["yougile"]


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(cfg["api_base"] + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Yougile said {e.code}: {e.read().decode()[:300]}")


company = settings.require("tasks.adapters.yougile.company_id")
login = input("Yougile login (email): ").strip()
password = getpass.getpass("Yougile password (hidden): ")
key = post("/auth/keys", {"login": login, "password": password, "companyId": company})["key"]
secrets_config.set("YOUGILE_API_KEY", key)
print(f"Done: key saved to {secrets_config.PATH} (chmod 600).")
