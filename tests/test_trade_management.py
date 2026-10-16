"""Hand-built scenarios for the exit logic (stop/target ordering, breakeven ratchet timing, time exits)."""
import numpy as np

from apex_omega.engine import SessionEngine
from apex_omega.session import DailyContext

F = np.float32


def mk():
    eng = SessionEngine("2024-01-02", DailyContext(100.0, 300.0), True)
    for m in range(0, 20):                      # flat warm-up bars
        eng.on_open(m, F(100)); eng.on_close(m, F(100), F(100.5), F(99.5), F(100), F(100))
    return eng


def enter(eng, m, direction, pillar="P2_ORB_5m_Delta", stop=10.0):
    eng.pending.append(dict(pillar=pillar, direction=direction, signal_m=m - 1, fill_m=m, raw="fixed",
                            stop=F(stop), why="test"))
    return eng.on_open(m, F(100))


def bar(eng, m, o, h, l, c):
    return eng.on_close(m, F(o), F(h), F(l), F(c), F(100))


def test_stop_checked_before_target_in_same_bar():
    eng = mk(); enter(eng, 20, 1)              # long, stop 90, target 160 (6R)
    ev = bar(eng, 20, 100, 170, 85, 100)       # touches both: stop wins (conservative)
    assert [e.data["reason"] for e in ev if e.kind == "EXIT"] == ["STOP"]
    assert abs(eng.trades[0]["net_r"] - (-11.0 / 10.0)) < 1e-6   # -10 stop - 1.0 friction


def test_breakeven_ratchet_applies_from_next_bar_only():
    eng = mk(); enter(eng, 20, 1)              # BE trigger = +2R = 120, lock +0.3R = 103
    ev = bar(eng, 20, 100, 121, 99, 110)       # trigger reached, same bar low 99 > stop 90
    assert [e.kind for e in ev] == ["MOVE_STOP"] and abs(ev[0].data["new_sl"] - 103.0) < 1e-4
    ev = bar(eng, 21, 110, 111, 102, 105)      # low 102 < 103 -> stopped at the locked stop
    ex = [e for e in ev if e.kind == "EXIT"][0]
    assert ex.data["reason"] == "BE_STOP" and abs(ex.data["exit_px"] - 103.0) < 1e-4
    assert abs(eng.trades[0]["net_pts"] - 2.0) < 1e-6          # +3 locked - 1 friction


def test_ratchet_bar_that_also_breaks_the_old_stop_exits_at_old_stop():
    eng = mk(); enter(eng, 20, 1)
    ev = bar(eng, 20, 100, 125, 85, 100)       # hits +2R and the original stop in one bar: stop first
    assert [e.data["reason"] for e in ev if e.kind == "EXIT"] == ["STOP"]


def test_target_hit_and_short_side_mirror():
    eng = mk(); enter(eng, 20, -1)             # short: stop 110, target 40
    ev = bar(eng, 20, 100, 105, 39, 50)
    ex = [e for e in ev if e.kind == "EXIT"][0]
    assert ex.data["reason"] == "TARGET" and abs(ex.data["net_pts"] - 59.0) < 1e-4


def test_time_exit_at_close_of_end_bar_ignores_that_bars_stop():
    eng = mk(); enter(eng, 20, 1, pillar="P1_OD10_Pullback")          # end_m = 365
    for m in range(20, 365):
        bar(eng, m, 100, 101, 99, 100)
    assert not eng.trades
    ev = bar(eng, 365, 100, 101, 50, 97)       # the end bar: exit at ITS CLOSE, stop not consulted (reference behaviour)
    ex = [e for e in ev if e.kind == "EXIT"][0]
    assert ex.data["reason"] == "TIME" and abs(ex.data["exit_px"] - 97.0) < 1e-4
    assert eng.p1_free_m == 368


def test_p1_wave2_only_after_winner_over_half_r():
    for net, waves in ((+0.6, 1), (+0.4, 2)):
        eng = mk(); enter(eng, 20, 1, pillar="P1_OD10_Pullback", stop=10.0)
        eng.positions[0].tp = F(1e9); eng.p1_waves = 1          # as set by the signal that opened this wave
        px = 100 + net * 10 + 1.0                                  # exit by time at this close
        for m in range(20, 365): bar(eng, m, 100, 101, 99, 100)
        bar(eng, 365, 100, 101, 99, px)
        assert eng.p1_waves == waves


def _uptrend_engine(last_m, pullback_m):
    """Steady uptrend with a single pullback bar at pullback_m; returns the engine after bar last_m closed."""
    eng = SessionEngine("2024-01-02", DailyContext(0.0, 300.0), True)
    for m in range(0, last_m + 1):
        c = 100.0 + 0.25 * m
        lo = c - 3.0 if m == pullback_m else c - 0.5
        eng.on_open(m, F(c - 0.25))
        eng.on_close(m, F(c - 0.25), F(c + 0.1), F(lo), F(c), F(100))
    return eng


def _p6_signals(eng):
    return [p for p in eng.pending if p["pillar"].startswith("P6")]


def test_p6_signal_allowed_on_last_window_bar_355_but_not_356():
    assert _p6_signals(_uptrend_engine(355, 355)), "P6 must fire on a valid pullback at m=355"
    assert not _p6_signals(_uptrend_engine(356, 356)), "P6 window ends at m=355"
