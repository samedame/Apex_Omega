"""Bar-by-bar APEX-OMEGA engine.

The same class drives the historical replay (verification) and the live bot, so the
two cannot drift apart.  Every rule below is a line-for-line translation of
``stage10_apex_omega_v2.py`` (signals + ``sim_trade_si`` trade management); the
variable names of the reference are kept where possible.

Timeline of one minute bar ``m`` (index = minutes since 09:30 ET):

* ``on_open(m, open_px)``  -- bar m has just opened.  Signals raised on the close of
  bar m-1 are filled here at the *open of bar m* (the reference enters at ``O[m+1]``
  and sizes the stop from that open, so the stop is only knowable now).
* ``on_close(m, o, h, l, c, v)`` -- bar m has closed.  Open positions are managed
  with bar m's high/low, then new signals are evaluated on information up to and
  including bar m (strictly causal).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import params as P
from .session import DailyContext, SessionBars

F32 = np.float32


@dataclass
class Position:
    pillar: str
    direction: int
    signal_m: int
    entry_m: int
    entry_px: np.float32
    stop_dist_raw: np.float32          # np.clip output; this is the ledger's risk_pts
    stop_dist: float                   # max(float(stop_dist_raw), 8.0) as used by sim_trade_si
    sl: np.float32
    tp: np.float32
    end_m: int
    be_trig_r: Optional[float]
    be_lock_r: float
    sl0: np.float32 = None
    be_active: bool = False
    emit: bool = True                  # False for trades removed by the High-Precision filter
    closed: bool = False


@dataclass
class Event:
    kind: str                          # ENTRY | MOVE_STOP | EXIT
    m: int                             # bar index the event is attached to
    pillar: str
    direction: int
    emit: bool
    data: dict = field(default_factory=dict)

    @property
    def key(self):
        d = self.data
        return f"{self.kind}|{self.pillar}|{d.get('entry_m')}|{self.m}"


class SessionEngine:
    def __init__(self, date: str, daily: DailyContext, high_precision: bool = True):
        self.date = date
        self.daily = daily
        self.hp = high_precision
        self.S = SessionBars()
        self.positions: list[Position] = []
        self.pending: list[dict] = []
        self.events: list[Event] = []
        self.trades: list[dict] = []            # closed trades in oracle ledger format (all, unfiltered)
        # per-day pillar state (names mirror the reference loop)
        self.p1_waves = 0
        self.p1_free_m = 11
        self.p2_done = False
        self.p3_done = False     # gate used by P4 in the reference
        self.p4_done = False     # gate used by P5
        self.p5_waves = 0        # gate used by P6
        self.p5_free_m = 240
        self.p6_done = False     # gate used by P3
        self._orb = {}
        self.has_drive10 = False
        self.drive10_dir = 0
        self.c30_ok = None

    # ------------------------------------------------------------------ open
    def on_open(self, m: int, open_px) -> list[Event]:
        """Fill signals raised on the previous bar's close."""
        out: list[Event] = []
        todo = [p for p in self.pending if p["fill_m"] == m]
        self.pending = [p for p in self.pending if p["fill_m"] != m]
        O1 = F32(open_px)
        for p in todo:
            direction, pillar = p["direction"], p["pillar"]
            if p["raw"] == "fixed":                       # ORB pillars: stop from the opening-range width
                stop_dist = p["stop"]
            else:                                         # P1/P5/P6: stop from the signal bar + the fill price
                base = p["base"]
                raw_sl = ((O1 - base) if direction == 1 else (base - O1)) + p["k"] * p["atr"]
                stop_dist = np.clip(raw_sl, p["min_sl"], p["max_sl"])
            spec = P.PILLARS[pillar]
            sd = max(float(stop_dist), P.MIN_STOP_FLOOR)
            sl = O1 - direction * sd
            tp = O1 + direction * (stop_dist * spec["target_r"])
            if pillar == "P5_VWAP_3SD_Rev":
                end_m = min(min(p["signal_m"] + 75, 380), P.M_BARS - 1)
            else:
                end_m = min(spec["exit_m"], P.M_BARS - 1)
            assert m < end_m, "entry must precede the time exit"
            risk = float(stop_dist)
            emit = True
            if self.hp:
                if pillar == "P2_ORB_5m_Delta" and risk > P.HP_P2_MAX_RISK:
                    emit = False
                if risk > P.HP_MAX_RISK:
                    emit = False
            pos = Position(pillar, direction, p["signal_m"], m, O1, stop_dist, sd, sl, tp, end_m,
                           spec["be_trig"], spec["be_lock"], sl0=sl, emit=emit)
            self.positions.append(pos)
            out.append(Event("ENTRY", m, pillar, direction, emit, dict(
                entry_m=m, signal_m=p["signal_m"], entry_px=float(O1), stop_dist=float(stop_dist),
                sl=float(sl), tp=float(tp), end_m=end_m, target_r=spec["target_r"],
                be_trig_px=(float(O1 + direction * (spec["be_trig"] * sd)) if spec["be_trig"] is not None else None),
                be_lock_px=(float(O1 + direction * (spec["be_lock"] * sd)) if spec["be_trig"] is not None else None),
                be_trig_r=spec["be_trig"], be_lock_r=spec["be_lock"], why=p.get("why", ""))))
        self.events.extend(out)
        return out

    # ----------------------------------------------------------------- close
    def on_close(self, m: int, o, h, l, c, v) -> list[Event]:
        if m != self.S.n:
            raise ValueError(f"expected bar {self.S.n}, got {m}")
        self.S.add(m, o, h, l, c, v)
        out: list[Event] = []
        out += self._manage(m)
        self._signals(m)
        self.events.extend(out)
        return out

    # ----------------------------------------------------------- trade mgmt
    def _manage(self, m: int) -> list[Event]:
        S = self.S
        out: list[Event] = []
        bh, bl, bc = S.H[m], S.L[m], S.C[m]
        for pos in list(self.positions):
            if pos.closed:
                continue
            ex = None  # (net_pts, reason)
            if m < pos.end_m:                       # reference: for m in range(entry_m, end_m)
                if pos.direction == 1:
                    if bl <= pos.sl:
                        ex = ((pos.sl - pos.entry_px) - P.FRICTION_PTS, "BE_STOP" if pos.be_active else "STOP", pos.sl)
                    elif bh >= pos.tp:
                        ex = ((pos.tp - pos.entry_px) - P.FRICTION_PTS, "TARGET", pos.tp)
                    elif pos.be_trig_r is not None and (bh - pos.entry_px) >= pos.be_trig_r * pos.stop_dist:
                        new_sl = max(pos.sl, pos.entry_px + pos.be_lock_r * pos.stop_dist)
                        pos.sl = new_sl
                        if not pos.be_active:
                            out.append(self._move_stop(pos, m, new_sl))
                else:
                    if bh >= pos.sl:
                        ex = ((pos.entry_px - pos.sl) - P.FRICTION_PTS, "BE_STOP" if pos.be_active else "STOP", pos.sl)
                    elif bl <= pos.tp:
                        ex = ((pos.entry_px - pos.tp) - P.FRICTION_PTS, "TARGET", pos.tp)
                    elif pos.be_trig_r is not None and (pos.entry_px - bl) >= pos.be_trig_r * pos.stop_dist:
                        new_sl = min(pos.sl, pos.entry_px - pos.be_lock_r * pos.stop_dist)
                        pos.sl = new_sl
                        if not pos.be_active:
                            out.append(self._move_stop(pos, m, new_sl))
            else:                                   # m == end_m: time exit at the bar's close
                net_pts = (bc - pos.entry_px) * pos.direction - P.FRICTION_PTS
                ex = (net_pts, "TIME", bc)
            if ex is not None:
                net_pts, reason, px = ex
                out.append(self._close(pos, m, net_pts, reason, px))
        return out

    def _move_stop(self, pos: Position, m: int, new_sl) -> Event:
        pos.be_active = True
        return Event("MOVE_STOP", m, pos.pillar, pos.direction, pos.emit, dict(
            entry_m=pos.entry_m, new_sl=float(new_sl), lock_r=pos.be_lock_r, entry_px=float(pos.entry_px)))

    def _close(self, pos: Position, m: int, net_pts, reason: str, px) -> Event:
        pos.closed = True
        net_r = net_pts / pos.stop_dist
        net_pts = F32(net_pts)
        self.trades.append(dict(date=self.date, entry_m=pos.entry_m, exit_m=m, dir=pos.direction,
                                risk_pts=pos.stop_dist_raw, net_pts=net_pts, net_r=net_r, pillar=pos.pillar,
                                exit_reason=reason, emit=pos.emit, entry_px=pos.entry_px, exit_px=px))
        if pos.pillar == "P1_OD10_Pullback":
            self.p1_free_m = m + 3
            if net_r <= 0.5:
                self.p1_waves = 2
        return Event("EXIT", m, pos.pillar, pos.direction, pos.emit, dict(
            entry_m=pos.entry_m, exit_px=float(px), reason=reason, net_pts=float(net_pts), net_r=float(net_r),
            entry_px=float(pos.entry_px), risk_pts=float(pos.stop_dist_raw), exit_m=m))

    # --------------------------------------------------------------- signals
    def _queue(self, m, pillar, direction, **kw):
        self.pending.append(dict(pillar=pillar, direction=direction, signal_m=m, fill_m=m + 1, **kw))

    def _signals(self, m: int):
        S = self.S
        O, H, L, C = S.O, S.H, S.L, S.C
        if m == 10:                                           # opening-drive regime, known at the 09:40 close
            drive10_pts = C[10] - O[0]
            self.has_drive10 = bool(abs(drive10_pts) >= F32(1.8) * S.ATR14[10])
            self.drive10_dir = 1 if drive10_pts > 0 else -1
        if m == 4:
            self._orb["5"] = (np.max(H[:5]), np.min(L[:5]))
        if m == 14:
            self._orb["15"] = (np.max(H[:15]), np.min(L[:15]))
        if m == 19:
            self._orb["20"] = (np.max(H[:20]), np.min(L[:20]))
        if m < P.SIGNAL_M_FIRST or m > P.SIGNAL_M_LAST:
            return
        if m + 1 >= P.M_BARS:
            return
        d = self.daily
        atr = max(S.ATR14[m], 4.0)
        min_sl = max(1.0 * atr, 10.0)
        max_sl = max(3.8 * atr, 35.0)
        EMA9, EMA21, CLV, VWAP, VWAP_STD = S.EMA9, S.EMA21, S.CLV, S.VWAP, S.VWAP_STD
        RSI5, RVOL, CUM_DELTA = S.RSI5, S.RVOL, S.CUM_DELTA

        # Pillar 1 -- 10m opening drive + EMA9 pullback (wave 2 only after a > +0.5R wave 1)
        if self.p1_waves < 2 and self.has_drive10 and self.p1_free_m <= m <= 135:
            direction = 0
            if self.drive10_dir == 1 and L[m] <= EMA9[m] and C[m] > EMA9[m] and CLV[m] > 0.40 and EMA9[m] > EMA21[m]:
                direction = 1; base = min(L[m], L[m - 1])
            elif self.drive10_dir == -1 and H[m] >= EMA9[m] and C[m] < EMA9[m] and CLV[m] < -0.40 and EMA9[m] < EMA21[m]:
                direction = -1; base = max(H[m], H[m - 1])
            if direction != 0:
                self._queue(m, "P1_OD10_Pullback", direction, raw="base", base=base, k=0.75, atr=atr,
                            min_sl=min_sl, max_sl=max_sl, why="drive10 + EMA9 rejection")
                self.p1_waves += 1
                self.p1_free_m = 10 ** 6                      # reopened by _close() at exit_m + 3

        # Pillar 2 -- 5m delta-confirmed ORB
        if not self.p2_done and 5 <= m <= 45 and "5" in self._orb:
            orb5_h, orb5_l = self._orb["5"]; orb5_rng = orb5_h - orb5_l
            if 1.2 * atr <= orb5_rng <= 0.45 * d.atr14:
                c_px = C[m]; direction = 0
                if c_px > orb5_h and CLV[m] > 0.45 and c_px > VWAP[m] and CUM_DELTA[m] > 0:
                    direction = 1
                elif c_px < orb5_l and CLV[m] < -0.45 and c_px < VWAP[m] and CUM_DELTA[m] < 0 and RVOL[m] > 1.30:
                    direction = -1
                if direction != 0:
                    stop_dist = np.clip(orb5_rng * 0.75, min_sl, max_sl)
                    self._queue(m, "P2_ORB_5m_Delta", direction, raw="fixed", stop=stop_dist, why="5m ORB break + VWAP + delta")
                    self.p2_done = True

        # Pillar 3 -- 15m asymmetric ORB (gate flag is p6_done in the reference)
        if not self.p6_done and 15 <= m <= 90 and "15" in self._orb:
            orb15_h, orb15_l = self._orb["15"]; orb15_rng = orb15_h - orb15_l
            if 1.2 * atr <= orb15_rng <= 0.55 * d.atr14:
                c_px = C[m]; direction = 0
                if c_px > orb15_h and CLV[m] > 0.45 and c_px > VWAP[m]:
                    direction = 1
                elif c_px < orb15_l and CLV[m] < -0.45 and O[0] < d.sma20 and RVOL[m] > 1.40:
                    direction = -1
                if direction != 0:
                    stop_dist = np.clip(orb15_rng * 1.0, min_sl, max_sl * 1.2)
                    self._queue(m, "P3_ORB_15m_Asym", direction, raw="fixed", stop=stop_dist, why="15m ORB break")
                    self.p6_done = True

        # Pillar 4 -- 20m HTF-aligned ORB (gate flag is p3_done in the reference)
        if not self.p3_done and 20 <= m <= 95 and "20" in self._orb:
            orb20_h, orb20_l = self._orb["20"]; orb20_rng = orb20_h - orb20_l
            if 1.2 * atr <= orb20_rng <= 0.55 * d.atr14:
                c_px = C[m]; direction = 0
                if c_px > orb20_h and CLV[m] > 0.45 and O[0] > d.sma20:
                    direction = 1
                elif c_px < orb20_l and CLV[m] < -0.45 and O[0] < d.sma20:
                    direction = -1
                if direction != 0:
                    stop_dist = np.clip(orb20_rng * 0.75, min_sl, max_sl)
                    self._queue(m, "P4_ORB_20m_HTF", direction, raw="fixed", stop=stop_dist, why="20m ORB break, SMA20-aligned")
                    self.p3_done = True

        # Pillar 5 -- session VWAP +/- 3 SD rubber band (gate flag is p4_done in the reference)
        if not self.p4_done and 45 <= m <= 325 and abs(C[30] - O[0]) <= 2.8 * S.ATR14[30]:
            up_b = VWAP[m] + 3.0 * VWAP_STD[m]
            dn_b = VWAP[m] - 3.0 * VWAP_STD[m]
            direction = 0
            if L[m] < dn_b and RSI5[m - 1] < 20 and C[m] > O[m] and CLV[m] > 0.45 and O[0] > d.sma20:
                direction = 1; base = L[m]
            elif H[m] > up_b and RSI5[m - 1] > 78 and C[m] < O[m] and CLV[m] < -0.45:
                direction = -1; base = H[m]
            if direction != 0:
                self._queue(m, "P5_VWAP_3SD_Rev", direction, raw="base", base=base, k=0.8, atr=atr,
                            min_sl=min_sl, max_sl=max_sl, why="3 SD VWAP extreme + RSI5 exhaustion")
                self.p4_done = True

        # Pillar 6 -- afternoon new-session-high/low EMA9 pullback (gate flag is p5_waves)
        if self.p5_waves < 1 and self.p5_free_m <= m <= 355:
            sess_h = np.max(H[:m - 5]); sess_l = np.min(L[:m - 5])
            rec_h = np.max(H[m - 10:m]); rec_l = np.min(L[m - 10:m])
            direction = 0
            if rec_h >= sess_h and L[m] <= EMA9[m] and C[m] > EMA9[m] and CLV[m] > 0.45 and EMA9[m] > EMA21[m] and C[m] > VWAP[m]:
                direction = 1; base = min(L[m], L[m - 1])
            elif rec_l <= sess_l and H[m] >= EMA9[m] and C[m] < EMA9[m] and CLV[m] < -0.45 and EMA9[m] < EMA21[m] and C[m] < VWAP[m]:
                direction = -1; base = max(H[m], H[m - 1])
            if direction != 0 and m + 1 < 388:
                self._queue(m, "P6_PM_NSH_Pullback", direction, raw="base", base=base, k=0.8, atr=atr,
                            min_sl=min_sl, max_sl=max_sl, why="new session extreme + EMA9 pullback")
                self.p5_waves += 1
