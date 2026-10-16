import datetime as dt

from apex_omega.providers import CsvTailProvider, YahooProvider
from apex_omega.timeutil import ET, UTC


class FakeResp:
    def __init__(self, body): self._b = body; self.status_code = 200
    def raise_for_status(self): pass
    def json(self): return self._b


class FakeSession:
    def __init__(self, body): self.body = body; self.headers = {}
    def get(self, url, params=None, timeout=None): return FakeResp(self.body)


def test_yahoo_parsing_nulls_alignment_and_forming_bar():
    t0 = int(dt.datetime(2026, 10, 16, 9, 30, tzinfo=ET).astimezone(UTC).timestamp())
    body = {"chart": {"result": [{"timestamp": [t0, t0 + 60, t0 + 120, t0 + 143],
            "indicators": {"quote": [{"open": [1, 2, None, 4], "high": [2, 3, None, 5], "low": [0, 1, None, 3],
                                      "close": [1.5, 2.5, None, 4.5], "volume": [10, 20, None, 5]}]}}]}}
    snap = YahooProvider(session=FakeSession(body)).fetch()
    assert [b.ts for b in snap.bars] == [t0, t0 + 60, t0 + 120]            # null minute skipped; last tick floored to its minute
    assert snap.bars[-1].o == 4 and snap.last_tick_ts == t0 + 143


def test_csv_provider_reads_iso_and_epoch(tmp_path):
    p = tmp_path / "bars.csv"
    p.write_text("timestamp,open,high,low,close,volume\n2026-10-16T13:30:00Z,100,101,99,100.5,50\n1792157460,100.5,102,100,101,60\n")
    snap = CsvTailProvider(str(p)).fetch()
    assert len(snap.bars) == 2 and snap.bars[0].ts % 60 == 0 and snap.bars[1].c == 101
