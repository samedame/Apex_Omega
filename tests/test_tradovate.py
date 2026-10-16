"""Tradovate provider: protocol, auth, contract roll, safety guards.  No network, no threads."""
import datetime as dt
import json

import pytest

from apex_omega.providers import ProviderError, make_provider
from apex_omega.tradovate import (TradovateAuth, TradovateFatal, TradovatePermanent, TradovateProvider,
                                  _Reconnect, front_month_symbol)

T0 = 1792157400.0            # 2026-10-16 13:30:00 UTC = 09:30 ET


class Resp:
    def __init__(self, body, status=200): self._b, self.status_code = body, status
    def json(self): return self._b


class FakeHttp:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def _next(self, kind, url, **kw):
        self.calls.append((kind, url, kw))
        return self.replies.pop(0)
    def post(self, url, **kw): return self._next("POST", url, **kw)
    def get(self, url, **kw): return self._next("GET", url, **kw)


class FakeTransport:
    def __init__(self): self.sent, self.frames, self.closed = [], [], False
    def connect(self): pass
    def send(self, s): self.sent.append(s)
    def recv(self): return self.frames.pop(0) if self.frames else None
    def close(self): self.closed = True


def tok(n="A", exp="2026-10-16T15:00:00.000Z"):
    return {"accessToken": f"acc-{n}", "mdAccessToken": f"md-{n}", "expirationTime": exp}


def bar(ts, o, h, l, c, up=3.0, dn=2.0):
    iso = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"timestamp": iso, "open": o, "high": h, "low": l, "close": c, "upVolume": up, "downVolume": dn}


def make(clock=lambda: T0, http=None, **kw):
    t = FakeTransport()
    p = TradovateProvider(symbol="NQZ6", token="seed", http=http or FakeHttp([Resp(tok())]), transport_factory=lambda: t,
                          clock=clock, autostart=False, **kw)
    p._connect()
    return p, t


def a(*msgs): return "a" + json.dumps(list(msgs))


def test_front_month_roll():
    assert front_month_symbol("NQ", dt.date(2026, 10, 16)) == "NQZ6"
    assert front_month_symbol("NQ", dt.date(2026, 9, 9)) == "NQU6"          # 3rd Fri Sep 18 -> rolls Sep 10
    assert front_month_symbol("MNQ", dt.date(2026, 9, 10)) == "MNQZ6"
    assert front_month_symbol("NQ", dt.date(2026, 12, 10)) == "NQH7"        # across the year boundary


def test_handshake_subscribes_minute_chart_and_merges_bars():
    now = [T0 + 150]
    p, t = make(clock=lambda: now[0])
    p._handle_frame("o")
    assert t.sent[0] == "authorize\n0\n\nmd-A"                              # md token, request id 0, blank params line
    p._handle_frame(a({"s": 200, "i": 0}))
    kind, rid, _, body = t.sent[1].split("\n", 3)
    assert (kind, rid) == ("md/getChart", "1")
    req = json.loads(body)
    assert req["symbol"] == "NQZ6" and req["chartDescription"]["underlyingType"] == "MinuteBar" and req["chartDescription"]["elementSize"] == 1
    p._handle_frame(a({"s": 200, "i": 1, "d": {"historicalId": 7, "realtimeId": 8}}))
    assert p._sub_ids == [7, 8]
    assert not p._ready.is_set()
    p._handle_frame(a({"e": "chart", "d": {"charts": [{"id": 7, "td": 20261016, "eoh": True,
                    "bars": [bar(T0, 100, 101, 99, 100.5), bar(T0 + 60, 100.5, 102, 100, 101)]}]}}))
    assert p._ready.is_set()
    snap = p.fetch()
    assert [b.ts for b in snap.bars] == [T0, T0 + 60] and snap.bars[0].v == 5.0         # volume = up + down
    # real-time update of the forming bar replaces it (same minute), a new minute appends
    p._handle_frame(a({"e": "chart", "d": {"charts": [{"id": 8, "bars": [bar(T0 + 60, 100.5, 103, 100, 102.5)]}]}}))
    p._handle_frame(a({"e": "chart", "d": {"charts": [{"id": 8, "bars": [bar(T0 + 120, 102.5, 102.75, 102.5, 102.75)]}]}}))
    s = p.snapshot()
    assert [(b.ts, b.h, b.c) for b in s.bars] == [(T0, 101, 100.5), (T0 + 60, 103, 102.5), (T0 + 120, 102.75, 102.75)]
    assert s.last_bar_forming and s.last_tick_ts == T0 + 150 and s.source == "tradovate:NQZ6"


def test_rejections_are_loud():
    p, _ = make()
    p._handle_frame("o")
    with pytest.raises(TradovateFatal):
        p._handle_frame(a({"s": 401, "i": 0, "d": "bad token"}))
    p2, _ = make()
    p2._handle_frame("o"); p2._handle_frame(a({"s": 200, "i": 0}))
    with pytest.raises(TradovateFatal, match="getChart failed"):
        p2._handle_frame(a({"s": 404, "i": 1, "d": "unknown symbol"}))


def test_bar_end_timestamps_are_refused():
    """If Tradovate stamped bars with their END, the forming bar would be in the future: never emit shifted bars."""
    p, _ = make(clock=lambda: T0 + 30)
    with pytest.raises(TradovatePermanent):
        p._on_chart({"bars": [bar(T0 + 60, 1, 2, 0, 1)]})
    assert p.snapshot().bars == []


def test_server_close_and_silence_trigger_reconnect():
    now = [T0]
    p, t = make(clock=lambda: now[0])
    with pytest.raises(_Reconnect):
        p._handle_frame('c[1000,"bye"]')
    now[0] = T0 + 13                                                         # nothing, not even heartbeats
    with pytest.raises(_Reconnect, match="no frames"):
        p._pump_once()


def test_heartbeat_sent_and_chart_watchdog():
    now = [T0]
    p, t = make(clock=lambda: now[0])
    t.frames = ["h"]
    now[0] = T0 + 3
    p._pump_once()
    assert "[]" in t.sent                                                    # client heartbeat (server needs one every 2.5 s)
    p._chart_id = 1
    now[0] = T0 + 50; t.frames = ["h"]
    with pytest.raises(_Reconnect, match="no chart data"):
        p._pump_once()


def test_token_refresh_forces_reconnect_before_expiry():
    now = [T0]
    p, t = make(clock=lambda: now[0], http=FakeHttp([Resp(tok(exp="2026-10-16T14:30:00.000Z")), Resp(tok("B", "2026-10-16T16:00:00.000Z"))]))
    now[0] = T0 + 40 * 60                                                    # token issued for 60 min: 20 min left, still fine
    t.frames = ["h"]; p._last_rx = now[0]
    p._pump_once()
    now[0] = T0 + 50 * 60                                                    # 10 min left (< 15) -> renew
    t.frames = ["h"]; p._last_rx = now[0]
    with pytest.raises(_Reconnect, match="token"):
        p._pump_once()
    p._connect()                                                             # reconnect path renews the token first
    assert p.auth.access == "acc-B" and p._sub_ids == []


def test_auth_prefers_renew_then_credentials_and_hides_secrets():
    calls = FakeHttp([Resp({"errorText": "expired"}, 401), Resp(tok("C"))])
    au = TradovateAuth(calls, "https://live.tradovateapi.com/v1", dict(name="u", password="PW-SECRET", cid="8", sec="SEC-SECRET"),
                       "old-token", clock=lambda: T0)
    au.ensure()
    assert [c[0] for c in calls.calls] == ["GET", "POST"] and au.access == "acc-C"
    body = calls.calls[1][2]["json"]
    assert body["cid"] == 8 and body["name"] == "u"
    # token only, nothing else configured and token dead -> clear fatal message, secrets never echoed
    au2 = TradovateAuth(FakeHttp([Resp({"errorText": "expired"}, 401)]), "https://x/v1", {}, "old-token", clock=lambda: T0)
    with pytest.raises(TradovateFatal) as e:
        au2.ensure()
    assert "old-token" not in str(e.value) and "TRADOVATE_NAME" in str(e.value)


def test_auth_rate_limit_ticket_is_honoured_and_captcha_is_fatal():
    slept = []
    http = FakeHttp([Resp({"p-ticket": "tk", "p-time": 3}), Resp(tok("D"))])
    au = TradovateAuth(http, "https://x/v1", dict(name="u", password="p", cid="8", sec="s"), None, clock=lambda: T0, sleep=slept.append)
    au.ensure()
    assert slept == [3.0] and http.calls[1][2]["json"]["p-ticket"] == "tk"
    http2 = FakeHttp([Resp({"p-ticket": "tk", "p-time": 3, "p-captcha": True})])
    au = TradovateAuth(http2, "https://x/v1", dict(name="u", password="p", cid="8", sec="s"), None, clock=lambda: T0, sleep=slept.append)
    with pytest.raises(TradovateFatal, match="captcha"):
        au.ensure()


def test_factory_and_env(monkeypatch):
    for k in ("TRADOVATE_ACCESS_TOKEN", "TRADOVATE_TOKEN", "TRADOVATE_NAME", "TRADOVATE_PASSWORD", "TRADOVATE_CID", "TRADOVATE_SEC"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ProviderError, match="TRADOVATE_ACCESS_TOKEN"):
        make_provider("tradovate")
    monkeypatch.setenv("TRADOVATE_ACCESS_TOKEN", "x")
    p = make_provider("tradovate:MNQ")
    assert p.root == "MNQ" and p._explicit_symbol is None and p.env == "live"
    assert make_provider("tradovate:nqz6")._explicit_symbol == "NQZ6"
    monkeypatch.setenv("TRADOVATE_ENV", "demo")
    assert make_provider("tradovate").env == "demo"


# ---------------------------------------------------------------- end to end through the real Runner
def test_runner_alerts_identical_when_bars_arrive_as_tradovate_chart_packets(sessions, oracle, monkeypatch):
    """Same golden days as test_live_path, but every poll's bars are round-tripped through Tradovate chart
    packets (ISO timestamps, up/down volume, history packet + forming-bar update).  Alerts must still match
    the backtest trade for trade, with no 'DELAYED' flag."""
    import harness as H
    from verify_against_backtest import check_live_day, hp_filter

    class ViaTradovate(H.SimProvider):
        def fetch(self):
            snap = super().fetch()
            p = TradovateProvider(symbol="NQZ6", token="x", http=FakeHttp([]), clock=lambda: self.cursor, autostart=False)
            bars = [bar(b.ts, b.o, b.h, b.l, b.c, up=b.v, dn=0.0) for b in snap.bars]
            if bars:
                p._on_chart({"eoh": True, "bars": bars[:-1]})
                p._on_chart({"bars": bars[-1:]})
            return p.snapshot()

    monkeypatch.setattr(H, "SimProvider", ViaTradovate)
    hp = hp_filter(oracle)
    busy = hp.groupby("date").size().sort_values(ascending=False).index[:2].tolist()
    dates = [d for d, _ in sessions]
    checked = 0
    for d in busy:
        i = dates.index(d)
        if i < 3:
            continue
        _, notifier, log = H.simulate_live_day(d, sessions[i][1], sessions[max(0, i - 25):i])
        assert check_live_day(d, sessions[i][1], hp, log) == []
        assert not any("DELAYED" in str(p) for p in notifier.sent_payloads)
        checked += 1
    assert checked >= 1


def test_threaded_provider_against_a_scripted_server():
    """Real background thread + the real frame protocol against a fake server: fetch() blocks until history
    arrives, returns bars, and close() cancels the chart and shuts the socket."""
    import time as _t
    import queue

    class Server:
        def __init__(self): self.q, self.sent, self.closed = queue.Queue(), [], False
        def connect(self): self.q.put("o")
        def send(self, s):
            self.sent.append(s)
            kind, rid = s.split("\n")[:2]
            if kind == "authorize":
                self.q.put(a({"s": 200, "i": int(rid)}))
            elif kind == "md/getChart":
                now = int(_t.time()) // 60 * 60
                self.q.put(a({"s": 200, "i": int(rid), "d": {"historicalId": 5, "realtimeId": 6}}))
                self.q.put(a({"e": "chart", "d": {"charts": [{"id": 5, "eoh": True, "bars": [bar(now - 60, 1, 2, 0, 1), bar(now, 1, 3, 1, 2)]}]}}))
        def recv(self):
            try: return self.q.get(timeout=0.05)
            except queue.Empty: return None
        def close(self): self.closed = True

    srv = Server()
    p = TradovateProvider(symbol="NQZ6", token="seed", http=FakeHttp([Resp(tok(exp="2099-01-01T00:00:00.000Z"))]),
                          transport_factory=lambda: srv, ready_timeout=5)
    try:
        snap = p.fetch()
        assert len(snap.bars) == 2 and snap.source == "tradovate:NQZ6" and snap.last_tick_ts > 0
    finally:
        p.close()
    assert srv.closed and any(s.startswith("md/cancelChart") and '"subscriptionId": 5' in s for s in srv.sent)
