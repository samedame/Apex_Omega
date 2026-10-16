"""Small JSON-file state: sent-alert ledger (de-dupe across restarts), daily history, paper account."""
from __future__ import annotations

import json
import os
import tempfile


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class State:
    def __init__(self, root="state"):
        self.root = root

    # -- sent alerts per session date
    def sent(self, date):
        return set(_load(os.path.join(self.root, f"sent-{date}.json"), []))

    def mark_sent(self, date, key):
        s = self.sent(date); s.add(key)
        _atomic_write(os.path.join(self.root, f"sent-{date}.json"), sorted(s))

    # -- daily history (prior valid sessions' RTH open/high/low/close)
    def history(self):
        return _load(os.path.join(self.root, "daily_history.json"), [])

    def save_history(self, rows):
        rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])[-40:]
        _atomic_write(os.path.join(self.root, "daily_history.json"), rows)

    # -- paper account
    def paper(self):
        return _load(os.path.join(self.root, "paper_account.json"), None)

    def save_paper(self, obj):
        _atomic_write(os.path.join(self.root, "paper_account.json"), obj)
