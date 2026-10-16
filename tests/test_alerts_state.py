import json

import pytest
import requests

from apex_omega import alerts as A
from apex_omega.engine import Event
from apex_omega.state import State


class FakeResp:
    def __init__(self, code, body=None): self.status_code, self._b = code, body or {}
    def json(self): return self._b


class FakeHttp:
    def __init__(self, codes): self.codes, self.calls = list(codes), 0
    def post(self, url, params=None, json=None, timeout=None):
        self.calls += 1; c = self.codes.pop(0)
        return FakeResp(c, {"retry_after": 0.01})


def test_webhook_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    http = FakeHttp([429, 500, 204])
    assert A.Notifier("https://discord.example/webhook", session=http).send({"content": "x"})
    assert http.calls == 3


def test_webhook_gives_up_on_client_error(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    with pytest.raises(A.AlertError):
        A.Notifier("https://discord.example/webhook", session=FakeHttp([404])).send({"content": "x"})


def test_entry_message_contents_and_limits():
    import datetime as dt
    ev = Event("ENTRY", 40, "P1_OD10_Pullback", -1, True, dict(
        entry_m=40, entry_px=12606.0, stop_dist=20.48, sl=12626.48, tp=12524.07, end_m=365, target_r=4.0,
        be_trig_px=12569.1, be_lock_px=12600.88, be_trig_r=1.8, be_lock_r=0.25, why="x"))
    msg = A.entry_message(dt.date(2024, 3, 5), ev, dict(source="yahoo", lag_s=600, delayed=True), size=3, open_others=1)
    e = msg["embeds"][0]
    assert "SHORT" in e["title"] and len(e["title"]) < 256
    vals = {f["name"]: f["value"] for f in e["fields"]}
    assert "12,606.00" in vals["Entry (market, at bar open)"] and "12,626.48" in vals["Stop"]
    assert "DELAYED FEED" in e["footer"]["text"] and "3 MNQ" in vals["Suggested size (two-gear)"]
    assert all(len(f["value"]) <= 1024 for f in e["fields"])


def test_state_roundtrip_and_atomic(tmp_path):
    s = State(str(tmp_path))
    s.mark_sent("2024-01-02", "a"); s.mark_sent("2024-01-02", "b")
    assert s.sent("2024-01-02") == {"a", "b"} and State(str(tmp_path)).sent("2024-01-02") == {"a", "b"}
    s.save_history([dict(date="2024-01-01", open=1, high=2, low=0, close=1)] * 3)
    assert len(s.history()) == 1
