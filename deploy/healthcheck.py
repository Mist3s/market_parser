"""Готовность процесса и схемы SQLite; запросов к маркетплейсам нет."""

import json
import os
import sqlite3
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=3) as response:
    if json.load(response).get("ok") is not True:
        raise SystemExit("API is not ready")
with sqlite3.connect(f"file:{os.environ['MKTLINK_DB_PATH']}?mode=ro", uri=True) as db:
    db.execute("SELECT count(*) FROM api_key").fetchone()
