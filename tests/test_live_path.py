"""Drive the real Runner (alerts + de-dupe + restart) through golden days with a simulated clock."""
import harness as H
from verify_against_backtest import check_live_day, hp_filter


def _days(sessions, oracle):
    hp = hp_filter(oracle)
    busy = hp.groupby("date").size().sort_values(ascending=False).index[:2].tolist()
    gap = [d for d, b in sessions if any(x is None for x in b)][:1]
    return hp, busy + gap


def test_alerts_match_backtest_trades_and_timing(sessions, oracle):
    hp, days = _days(sessions, oracle)
    dates = [d for d, _ in sessions]
    for d in days:
        i = dates.index(d)
        if i < 3:
            continue
        _, notifier, log = H.simulate_live_day(d, sessions[i][1], sessions[max(0, i - 25):i])
        assert check_live_day(d, sessions[i][1], hp, log) == []
        keys = [str(p) for p in notifier.sent_payloads]
        assert len(keys) == len(set(keys)), "duplicate alert"


def test_restart_does_not_duplicate_or_lose_alerts(sessions, oracle):
    hp, days = _days(sessions, oracle)
    dates = [d for d, _ in sessions]
    d = [x for x in days if dates.index(x) >= 3][0]; i = dates.index(d)
    _, notifier, log = H.simulate_live_day(d, sessions[i][1], sessions[max(0, i - 25):i], offsets=(1, 20, 59), restart_every=37)
    assert check_live_day(d, sessions[i][1], hp, log) == []
