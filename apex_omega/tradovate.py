"""Tradovate real-time 1-minute bars (NQ / MNQ futures) behind the standard provider interface.

How it works
------------
* Auth (REST): ``POST /auth/accesstokenrequest`` with your credentials + API key, or, when only a bare
  access token is supplied, ``GET /auth/renewaccesstoken``.  Tokens live ~90 min and are renewed ~15 min
  early; the market-data socket is reconnected with the fresh token (the history request refills any gap).
* Market data: ONE persistent WebSocket (``wss://md.tradovateapi.com/v1/websocket``, Tradovate allows one
  simultaneous connection per session) in a background thread.  It subscribes ``md/getChart`` (MinuteBar,
  size 1): a historical packet (ends with ``eoh``) followed by real-time updates of the forming bar.
* ``fetch()`` is the same call the runner already makes to Yahoo: it returns the bars held in memory, so
  polling every few seconds costs nothing and never opens a new connection.

The bot only needs the OPEN of the forming bar (to price entries); closed bars are final.  The feed's
"last tick" time is the local receive time of the newest chart packet, so a silent/dead feed shows up as lag
and the existing DELAYED labelling in the alerts takes over automatically.

Licensing: Tradovate / CME data may be used for your own analysis and alerts only (no redistribution; do not
store it for more than 24 h).  Do not publish this bot's raw bars; the Discord alerts carry derived levels only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from typing import Callable, Optional

import requests

from .providers import Bar, ProviderError, Snapshot

REST = {"live": "https://live.tradovateapi.com/v1", "demo": "https://demo.tradovateapi.com/v1"}
MD_WS = "wss://md.tradovateapi.com/v1/websocket"

_MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}
ROLL_DAYS_BEFORE_EXPIRY = 8            # volume normally migrates ~8 days before the 3rd Friday
REFRESH_BEFORE_S = 15 * 60             # renew the token this long before it expires
HEARTBEAT_S = 2.0                      # server needs a frame at least every 2.5 s
NO_FRAME_S = 12.0                      # nothing at all (not even server heartbeats) -> reconnect
NO_CHART_S = 45.0                      # no chart packet during RTH -> resubscribe
MAX_BARS = 4000


# --------------------------------------------------------------------------- contract selection
def _third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    first_friday = d + dt.timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + dt.timedelta(days=14)


def front_month_symbol(root: str, today: dt.date, roll_days: int = ROLL_DAYS_BEFORE_EXPIRY) -> str:
    """Quarterly (H/M/U/Z) front month, rolled `roll_days` before the 3rd-Friday expiry. e.g. NQZ6."""
    for year in (today.year, today.year + 1):
        for month in (3, 6, 9, 12):
            roll = _third_friday(year, month) - dt.timedelta(days=roll_days)
            if today < roll:
                return f"{root}{_MONTH_CODE[month]}{year % 10}"
    raise ProviderError("could not derive a front-month contract")


# --------------------------------------------------------------------------- helpers
def _parse_iso(s: str) -> float:
    s = s.strip().replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def _short(x, n=200) -> str:
    return str(x)[:n]


class _Reconnect(Exception):
    """Internal: drop the socket and start a fresh one (not an error)."""


class TradovateFatal(ProviderError):
    """Auth/subscription rejected: retried a few times, then a 2-minute cool-down (never hammer a login)."""


class TradovatePermanent(TradovateFatal):
    """Never retry: the data would be silently wrong (e.g. bar timestamps shifted by a minute)."""


# --------------------------------------------------------------------------- auth
class TradovateAuth:
    def __init__(self, http, rest_base: str, creds: dict, token: Optional[str],
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.http, self.base, self.creds, self.clock, self.sleep = http, rest_base.rstrip("/"), creds, clock, sleep
        self._seed = token or None
        self.access: Optional[str] = None
        self.md: Optional[str] = None
        self.expires_at = 0.0

    def has_credentials(self) -> bool:
        return all(self.creds.get(k) for k in ("name", "password", "cid", "sec"))

    def needs_refresh(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        return self.access is None or self.expires_at - now < REFRESH_BEFORE_S

    def ensure(self) -> None:
        if not self.needs_refresh():
            return
        token = self.access or self._seed
        err = None
        if token:
            try:
                self._apply(self._renew(token))
                return
            except ProviderError as e:
                err = e
        if self.has_credentials():
            self._apply(self._token_request())
            return
        raise TradovateFatal(
            "Tradovate access token is missing/expired and no login credentials are configured "
            "(set TRADOVATE_NAME, TRADOVATE_PASSWORD, TRADOVATE_CID, TRADOVATE_SEC for unattended runs)"
            + (f" [{err}]" if err else ""))

    # -- REST calls (never put a secret into an exception message)
    def _renew(self, token: str) -> dict:
        try:
            r = self.http.get(f"{self.base}/auth/renewaccesstoken",
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=15)
            j = r.json()
        except Exception as e:                                                     # noqa: BLE001
            raise ProviderError(f"tradovate renew failed: {type(e).__name__}") from None
        if getattr(r, "status_code", 200) != 200 or "accessToken" not in j:
            raise ProviderError(f"tradovate renew rejected: {_short(j.get('errorText') or getattr(r, 'status_code', '?'))}")
        return j

    def _token_request(self) -> dict:
        c = self.creds
        body = dict(name=c["name"], password=c["password"], appId=c.get("app_id") or "apex-omega-bot",
                    appVersion="1.0", cid=int(c["cid"]), sec=c["sec"],
                    deviceId=c.get("device_id") or "apex-omega-bot-0001")
        for _ in range(4):
            try:
                r = self.http.post(f"{self.base}/auth/accesstokenrequest", json=body, timeout=15,
                                   headers={"Accept": "application/json"})
                j = r.json()
            except Exception as e:                                                 # noqa: BLE001
                raise ProviderError(f"tradovate auth request failed: {type(e).__name__}") from None
            if "p-ticket" in j:                                                    # rate limited: wait and resend
                if j.get("p-captcha"):
                    raise TradovateFatal("Tradovate requires a captcha for this login: sign in once at "
                                         "trader.tradovate.com from a browser, then retry")
                self.sleep(min(float(j.get("p-time", 1)), 60.0))
                body["p-ticket"] = j["p-ticket"]
                continue
            if j.get("errorText") or "accessToken" not in j:
                raise TradovateFatal(f"tradovate auth rejected: {_short(j.get('errorText') or 'no accessToken')}")
            return j
        raise ProviderError("tradovate auth still rate limited after retries")

    def _apply(self, j: dict) -> None:
        self.access = j["accessToken"]
        self.md = j.get("mdAccessToken") or self.access
        try:
            self.expires_at = _parse_iso(j["expirationTime"])
        except Exception:                                                          # noqa: BLE001
            self.expires_at = self.clock() + 85 * 60


# --------------------------------------------------------------------------- transport
class WsTransport:
    """Thin wrapper over websocket-client so tests can swap in a scripted fake."""

    def __init__(self, url: str, timeout: float = 0.5):
        self.url, self.timeout, self.ws = url, timeout, None

    def connect(self) -> None:
        import websocket
        self.ws = websocket.create_connection(self.url, timeout=10)
        self.ws.settimeout(self.timeout)

    def send(self, s: str) -> None:
        self.ws.send(s)

    def recv(self) -> Optional[str]:
        import websocket
        try:
            return self.ws.recv()
        except websocket.WebSocketTimeoutException:
            return None

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:                                                          # noqa: BLE001
            pass


# --------------------------------------------------------------------------- provider
class TradovateProvider:
    name = "tradovate"

    def __init__(self, symbol: Optional[str] = None, root: str = "NQ", env: str = "live", *,
                 token: Optional[str] = None, creds: Optional[dict] = None,
                 http=None, transport_factory: Optional[Callable[[], object]] = None,
                 md_url: str = MD_WS, history_bars: int = 700, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, autostart: bool = True, ready_timeout: float = 40.0,
                 today: Optional[Callable[[], dt.date]] = None):
        if env not in REST:
            raise ValueError(f"TRADOVATE_ENV must be live or demo, got {env!r}")
        self.root, self.env, self.clock, self.sleep = root, env, clock, sleep
        self._explicit_symbol = symbol
        self._today = today or (lambda: dt.datetime.fromtimestamp(self.clock(), tz=dt.timezone.utc).date())
        self.history_bars, self.ready_timeout, self.autostart = history_bars, ready_timeout, autostart
        self.http = http or requests.Session()
        self.auth = TradovateAuth(self.http, REST[env], creds or {}, token, clock, sleep)
        if not token and not self.auth.has_credentials():
            raise ProviderError("tradovate: set TRADOVATE_ACCESS_TOKEN or TRADOVATE_NAME/PASSWORD/CID/SEC")
        self._factory = transport_factory or (lambda: WsTransport(md_url))
        # runtime state (guarded by _lock)
        self._lock = threading.Lock()
        self._bars: dict = {}
        self._last_chart_rx = 0.0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t = None
        self._rid = 0
        self._auth_id = self._chart_id = None
        self._sub_ids: list = []
        self._symbol: Optional[str] = None
        self._last_rx = self._last_hb = self._last_chart_rx_conn = 0.0
        self.last_error = ""
        self._fatal: Optional[str] = None
        self._retry_at = 0.0
        self._fails = 0

    # ------------------------------------------------------------------ construction from env
    @classmethod
    def from_env(cls, spec: str = "tradovate", env=os.environ, **kw):
        arg = spec.split(":", 1)[1].strip().upper() if ":" in spec else ""
        symbol, root = None, env.get("TRADOVATE_ROOT") or "NQ"
        if arg:
            if any(ch.isdigit() for ch in arg):
                symbol = arg                                    # explicit contract, e.g. NQZ6
            else:
                root = arg                                      # product root, e.g. MNQ
        symbol = symbol or (env.get("TRADOVATE_SYMBOL") or None)
        creds = dict(name=env.get("TRADOVATE_NAME"), password=env.get("TRADOVATE_PASSWORD"), cid=env.get("TRADOVATE_CID"),
                     sec=env.get("TRADOVATE_SEC"), app_id=env.get("TRADOVATE_APP_ID"), device_id=env.get("TRADOVATE_DEVICE_ID"))
        token = env.get("TRADOVATE_ACCESS_TOKEN") or env.get("TRADOVATE_TOKEN") or None
        return cls(symbol=symbol, root=root, env=(env.get("TRADOVATE_ENV") or "live").lower(), token=token, creds=creds,
                   md_url=env.get("TRADOVATE_MD_URL") or MD_WS, **kw)

    # ------------------------------------------------------------------ public API
    def fetch(self) -> Snapshot:
        if self.autostart:
            self._ensure_thread()
        deadline = time.monotonic() + self.ready_timeout
        while not self._ready.is_set():
            if self._fatal:
                raise ProviderError(self._fatal)
            if time.monotonic() > deadline or (self.autostart and self._thread is not None and not self._thread.is_alive()):
                raise ProviderError(f"tradovate: no data yet ({self.last_error or 'timed out'})")
            if not self.autostart:
                raise ProviderError("tradovate: not connected")
            self._ready.wait(0.25)
        return self.snapshot()

    def snapshot(self) -> Snapshot:
        with self._lock:
            bars = [self._bars[k] for k in sorted(self._bars)]
            last = self._last_chart_rx
        return Snapshot(bars, last, self.clock(), f"{self.name}:{self._symbol or self.root}", last_bar_forming=True)

    def close(self) -> None:
        self._stop.set()
        t = self._t
        if t is not None:
            try:
                for sid in self._sub_ids:
                    t.send(f"md/cancelChart\n{self._next_id()}\n\n{json.dumps({'subscriptionId': sid})}")
            except Exception:                                                      # noqa: BLE001
                pass
            t.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ thread
    def _ensure_thread(self) -> None:
        if self._fatal:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        if self.clock() < self._retry_at:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tradovate-md", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect()
                backoff, self._fails = 1.0, 0
                while not self._stop.is_set():
                    self._pump_once()
            except _Reconnect as e:
                self.last_error = str(e)
                backoff = 1.0
            except TradovateFatal as e:
                self.last_error = str(e)
                self._fails += 1
                if isinstance(e, TradovatePermanent):
                    self._fatal = str(e)
                if self._fatal or self._fails >= 3:
                    self._retry_at = self.clock() + 120.0                          # cool-down before fetch() retries
                    self._close_transport()
                    return
            except Exception as e:                                                 # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {_short(e)}"
            self._close_transport()
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    def _close_transport(self) -> None:
        if self._t is not None:
            self._t.close()
            self._t = None

    # ------------------------------------------------------------------ connection
    def _next_id(self) -> int:
        self._rid += 1
        return self._rid

    def _resolve_symbol(self) -> str:
        return self._explicit_symbol or front_month_symbol(self.root, self._today())

    def _connect(self) -> None:
        self.auth.ensure()
        self._symbol = self._resolve_symbol()
        self._rid, self._sub_ids = -1, []
        self._auth_id = self._chart_id = None
        t = self._factory()
        t.connect()
        self._t = t
        self._last_rx = self._last_hb = self.clock()
        self._last_chart_rx_conn = self.clock()

    def _pump_once(self) -> None:
        raw = self._t.recv()
        now = self.clock()
        if raw:
            self._last_rx = now
            self._handle_frame(raw)
        if now - self._last_hb >= HEARTBEAT_S:
            self._t.send("[]")
            self._last_hb = now
        if now - self._last_rx > NO_FRAME_S:
            raise _Reconnect("no frames from the market-data socket")
        if self._chart_id is not None and now - max(self._last_chart_rx, self._last_chart_rx_conn) > NO_CHART_S:
            raise _Reconnect("no chart data for 45 s")
        if self.auth.needs_refresh(now):
            raise _Reconnect("refreshing access token")

    # ------------------------------------------------------------------ protocol
    def _handle_frame(self, raw: str) -> None:
        if raw == "o":
            self._auth_id = self._next_id()
            self._t.send(f"authorize\n{self._auth_id}\n\n{self.auth.md or self.auth.access}")
        elif raw == "h" or not raw:
            return
        elif raw[0] == "a":
            try:
                msgs = json.loads(raw[1:])
            except ValueError:
                return
            for m in msgs:
                self._on_message(m)
        elif raw[0] == "c":
            raise _Reconnect(f"server closed the socket: {_short(raw, 80)}")

    def _on_message(self, m: dict) -> None:
        if "i" in m and "s" in m:
            i, s = m["i"], m["s"]
            if i == self._auth_id:
                if s != 200:
                    raise TradovateFatal(f"tradovate md authorize failed ({s}): {_short(m.get('d'))}")
                self._subscribe()
            elif i == self._chart_id:
                if s != 200:
                    raise TradovateFatal(f"tradovate getChart failed for {self._symbol} ({s}): {_short(m.get('d'))}")
                d = m.get("d") or {}
                self._sub_ids = [x for x in (d.get("historicalId"), d.get("realtimeId")) if x is not None]
        elif m.get("e") == "chart":
            for ch in (m.get("d") or {}).get("charts", []):
                self._on_chart(ch)

    def _subscribe(self) -> None:
        self._chart_id = self._next_id()
        body = {"symbol": self._symbol,
                "chartDescription": {"underlyingType": "MinuteBar", "elementSize": 1,
                                     "elementSizeUnit": "UnderlyingUnits", "withHistogram": False},
                "timeRange": {"asMuchAsElements": int(self.history_bars)}}
        self._t.send(f"md/getChart\n{self._chart_id}\n\n{json.dumps(body)}")

    def _on_chart(self, ch: dict) -> None:
        now = self.clock()
        new = {}
        for b in ch.get("bars", []):
            try:
                ts = int(_parse_iso(b["timestamp"])) // 60 * 60
                o, h, l, c = (float(b[k]) for k in ("open", "high", "low", "close"))
            except (KeyError, TypeError, ValueError):
                continue
            if ts > now + 5:
                raise TradovatePermanent("tradovate bar timestamp is in the future: timestamps look like the bar END, "
                                     "not the start; refusing to emit shifted bars")
            v = float(b.get("upVolume") or 0) + float(b.get("downVolume") or 0)
            new[ts] = Bar(ts, o, h, l, c, v)
        with self._lock:
            if new:
                self._bars.update(new)
                self._last_chart_rx = now
                if len(self._bars) > MAX_BARS:
                    for k in sorted(self._bars)[:len(self._bars) - MAX_BARS]:
                        del self._bars[k]
        if ch.get("eoh"):
            self._ready.set()
