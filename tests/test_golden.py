"""The bot's engine reproduces the original backtest, trade for trade, bit for bit."""
import numpy as np

import harness as H
from apex_omega import params as P
from apex_omega.replay import replay_sessions


def test_replay_matches_original_backtest_exactly(sessions, oracle):
    led, _ = replay_sessions(iter(sessions), high_precision=False)
    cmp = H.compare_ledgers(led, oracle)
    assert cmp["identical"], cmp


def test_high_precision_filter_matches_stage12(sessions, oracle):
    led, _ = replay_sessions(iter(sessions), high_precision=True)
    hp = oracle[((oracle.pillar != "P2_ORB_5m_Delta") | (oracle.risk_pts <= 28.0)) & (oracle.risk_pts <= 68.0)]
    assert H.compare_ledgers(led[led.emit], hp)["identical"]
    assert 0 < len(hp) < len(oracle)


def test_golden_block_exercises_every_pillar_and_exit_type(sessions):
    led, _ = replay_sessions(iter(sessions), high_precision=False)
    assert set(led.pillar) == set(P.PILLARS)
    assert {"STOP", "BE_STOP", "TARGET", "TIME"} <= set(led.exit_reason)
    p1 = led[led.pillar == "P1_OD10_Pullback"].groupby("date").size()
    assert (p1 >= 2).any(), "block should contain a P1 wave-2"
