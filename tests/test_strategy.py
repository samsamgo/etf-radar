"""투자 계획(app/strategy.py) — 네트워크 없이 규칙 자체를 검사한다. 자료는 임시 DB 에 직접 넣는다."""
import json
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("ETF_DATA_DIR", os.path.join(os.path.dirname(__file__), "_tmp_plan"))
from app import db, strategy  # noqa: E402


def tdays(n: int, end: str = "2026-09-28") -> list[str]:
    """end 에서 거꾸로 n 거래일(주말·휴장일 제외)."""
    out, d = [], date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5 and d.isoformat() not in strategy.KRX_HOLIDAYS:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "t.db")
    c = db.connect()
    yield c
    c.commit(); c.close()


def seed_index(con, days: list[str], ks: list[float], kq: list[float] | None = None):
    con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", [("KS11", d, v) for d, v in zip(days, ks)])
    con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", [("KQ11", d, v) for d, v in zip(days, kq or ks)])


def seed_series(con, days: list[str], **kv):
    for k, vals in kv.items():
        con.executemany("INSERT OR REPLACE INTO series(key,date,value) VALUES(?,?,?)", [(k, d, v) for d, v in zip(days, vals)])


def test_calendar_month_end_and_next():
    cal = strategy.Calendar(["2026-08-28", "2026-08-31", "2026-09-01"])
    assert cal.is_month_end("2026-08-31") and not cal.is_month_end("2026-08-28")
    assert cal.next("2026-09-01") == "2026-09-02"
    assert cal.next("2026-09-23") == "2026-09-28"  # 추석 휴장(9/24·25) 건너뜀
    assert cal.last_month_end("2026-09-01") == "2026-08-31"
    assert cal.next_month_end("2026-09-01") == "2026-09-30"


def test_faber_turns_leg_on_only_above_10m_average(con):
    days = tdays(300)
    n = len(days)
    ks = [100 + i * 0.5 for i in range(n)]           # 꾸준히 오름 → 켜짐
    seed_index(con, days, ks)
    spx = [100.0] * n                                  # 평평 → 마지막 값 = 평균 → 켜지지 않음(> 조건)
    gold = [200 - i * 0.3 for i in range(n)]           # 내림 → 꺼짐
    usd = [1300.0] * n
    seed_series(con, days, spx=spx, gold_usd=gold, usdkrw=usd, vix=[15.0] * n)
    ctx = strategy.Ctx(con)
    r = strategy.faber(ctx, {})
    assert r["legs"]["KS"]["on"] is True
    assert r["legs"]["SPX"]["on"] is False
    assert r["legs"]["GOLD"]["on"] is False
    assert r["targets"] == {"069500": 23.33, "153130": 46.67}
    assert r["legs"]["KS"]["months"] == 10


def test_faber_keeps_previous_when_data_missing(con):
    days = tdays(300)
    seed_index(con, days, [100.0] * 300)
    seed_series(con, days, usdkrw=[1300.0] * 300, vix=[15.0] * 300)   # S&P·금 없음
    ctx = strategy.Ctx(con)
    r = strategy.faber(ctx, {"core": {"legs": {"SPX": {"on": True}, "GOLD": {"on": False}}}})
    assert r["legs"]["SPX"]["on"] is True and "지난 판정 유지" in r["legs"]["SPX"]["note"]
    assert r["legs"]["GOLD"]["on"] is False
    assert any("자료가 모자라" in x for x in ctx.notes)


def test_crash_signal_buys_next_close_and_sells_after_3_days(con):
    days = tdays(80)
    ks = [100.0] * 80
    ks[-3:] = [99.0, 98.0, 97.0]                       # 3일 −3%
    seed_index(con, days, ks)
    seed_series(con, days, usdkrw=[1300.0] * 80, vix=[15.0] * 80)
    ctx = strategy.Ctx(con)
    st = {}
    r = strategy.crash(ctx, st)
    assert r["signal"] and r["targets"] == {"069500": 6.0}
    hold_until = st["crash"]["hold_until"]
    assert hold_until == ctx.cal.shift(ctx.asof, 4)
    # 매도일 전날: 다음 거래일이 매도일이면 목표 0
    ctx2 = strategy.Ctx(con, days[-1])
    st["crash"]["hold_until"] = ctx2.cal.next(ctx2.asof)
    ks2 = [100.0] * 80
    con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", [("KS11", d, v) for d, v in zip(days, ks2)])
    ctx2 = strategy.Ctx(con, days[-1])
    r2 = strategy.crash(ctx2, st)
    assert r2["targets"] == {"153130": 6.0} and "매도" in r2["action"]


def test_combine_caps_and_dedups(con):
    days = tdays(70)
    seed_index(con, days, [100.0] * 70)
    seed_series(con, days, usdkrw=[1300.0] * 70)
    con.executemany("INSERT INTO stock_daily VALUES(?,?,?,?,?,?,?)",
                    [(days[-1], "000001", "가", "KOSPI", 1000, 1e9, 5e12), (days[-1], "000002", "나", "KOSDAQ", 500, 1e9, 5e11)])
    ctx = strategy.Ctx(con)
    S = {"core": {"targets": {"069500": 23.33, "153130": 46.67}}, "crash": {"targets": {"069500": 6.0}},
         "mom": {"targets": {"000001": 2.5, "000002": 2.5, "153130": 5.0}}, "etf": {"targets": {"000001": 0.4, "153130": 7.6}},
         "val": {"targets": {"153130": 6.0}}}
    C = strategy.combine(ctx, S)
    by = {r["code"]: r for r in C["rows"]}
    assert by["069500"]["pct"] == pytest.approx(29.33)
    assert by["000001"]["pct"] == 2.5 and C["duplicates"]                  # 겹친 종목은 큰 갈래만
    assert C["cash_pct"] == pytest.approx(46.67 + 5.0 + 7.6 + 0.4 + 6.0)   # 밀려난 몫은 그 갈래의 현금으로
    assert C["total_pct"] == pytest.approx(100.0)
    assert C["kospi_exposure"]["sum"] == pytest.approx(29.33)
    assert by["000001"]["price"] == 1000
    d = strategy.diff_targets({"date": days[-2], "targets": {"069500": 23.33, "153130": 76.67}}, C["rows"], ctx)
    sides = {o["code"]: o["side"] for o in d["orders"]}
    assert sides["069500"] == "BUY" and sides["153130"] == "SELL" and sides["000001"] == "BUY"


def test_momentum_list_picks_low_vol_from_pool_and_top_mcap(con):
    """규칙 그대로 독립 계산한 기대 집합과 같아야 한다: 전 종목 12-1 모멘텀 상위 200 ∩ 시총 상위 200 → 60일 변동성 최저 40."""
    days = tdays(300)
    seed_index(con, days, [100.0] * 300)
    seed_series(con, days, usdkrw=[1300.0] * 300)
    d = days[-1]
    import random
    import statistics
    rnd = random.Random(1)
    rows, snap, px = [], [], {}
    for k in range(400):
        code = f"{k:06d}"
        drift = 0.0015 if k < 150 else -0.002                # 앞 150 종목만 모멘텀 높음(12개월 +50%대 — 조작 스크린의 +100% 에 안 걸리게)
        vol = 0.003 if k % 2 == 0 else 0.012                 # 짝수 종목이 저변동
        p, series = 100.0, []
        for dd in days:
            p *= (1 + drift) * (1 + rnd.gauss(0, vol))
            rows.append((code, dd, p)); series.append(p)
        px[code] = series
        snap.append((d, code, f"n{k}", "KOSPI", p, 5e9, (400 - k) * 1e11))   # k 가 작을수록 시총 큼
    con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", rows)
    con.executemany("INSERT INTO stock_daily VALUES(?,?,?,?,?,?,?)", snap)
    ctx = strategy.Ctx(con)
    r = strategy.momentum_list(ctx, d)
    i = len(days) - 1
    mom = {c: s[i - 21] / s[i - 252] - 1 for c, s in px.items()}
    pool = set(sorted(mom, key=lambda c: -mom[c])[:200])
    top = {f"{k:06d}" for k in range(200)}
    vol60 = {c: statistics.pstdev([b / a - 1 for a, b in zip(s[-61:], s[-60:])]) for c, s in px.items()}
    expected = sorted(pool & top, key=lambda c: vol60[c])[:40]
    assert r["list"] == expected
    assert r["n_pool_in_universe"] == len(pool & top)
    assert all(c in pool and c in top and int(c) % 2 == 0 for c in r["list"])   # 풀·시총 안의 저변동 종목만 남는다


def test_lowpbr_uses_positive_eps_and_min_mcap(con):
    days = tdays(70)
    seed_index(con, days, [100.0] * 70)
    seed_series(con, days, usdkrw=[1300.0] * 70, vix=[10.0] * 70)
    d = days[-1]
    con.executemany("INSERT INTO stock_daily VALUES(?,?,?,?,?,?,?)",
                    [(d, "000001", "싼흑자", "KOSPI", 1000, 1e9, 2e12), (d, "000002", "싼적자", "KOSPI", 1000, 1e9, 2e12),
                     (d, "000003", "작은회사", "KOSPI", 1000, 1e9, 5e11), (d, "000004", "비싼흑자", "KOSPI", 1000, 1e9, 2e12)])
    con.executemany("INSERT INTO fund VALUES(?,?,?,?,?,?)",
                    [("000001", d, 0.4, 2500, 100, 10), ("000002", d, 0.3, 3000, -50, None), ("000003", d, 0.2, 5000, 100, 10), ("000004", d, 3.0, 300, 100, 30)])
    ctx = strategy.Ctx(con)
    r = strategy.lowpbr_list(ctx, d)
    assert r["list"] == ["000001", "000004"]
    out = strategy.lowpbr(ctx, {})
    assert out["gate_on"] and out["targets"]["000001"] == pytest.approx(0.2)
    assert out["targets"]["153130"] == pytest.approx(6.0 - 0.4)


def test_etf_leader_opens_positions_only_when_gate_on(con):
    days = tdays(300)
    n = len(days)
    seed_index(con, days, [100.0] * n, kq=[100 + i for i in range(n)])   # 코스닥 120일선 위
    seed_series(con, days, usdkrw=[1300.0] * n, vix=[15.0] * n)
    obs = days[-1]
    con.execute("INSERT INTO etf VALUES(?,?,?,?,?,?,?,?,?)", ("100000", "TIGER 로봇", 1, "로봇", 0, "", "미래에셋", obs, "국내주식형, 섹터"))
    con.execute("INSERT INTO change VALUES(?,?,?,?,?,?,?,?,?)", (obs, "100000", "000001", "가", "UP", 0.5, 1.0, 1.6, 1e9))
    con.execute("INSERT INTO change VALUES(?,?,?,?,?,?,?,?,?)", (obs, "100000", "000002", "나", "UP", 0.02, 1.0, 1.02, 1e8))  # +2% 는 추가로 안 침
    con.executemany("INSERT INTO stock_daily VALUES(?,?,?,?,?,?,?)",
                    [(obs, "000001", "가", "KOSPI", 200, 5e9, 5e11), (obs, "000002", "나", "KOSPI", 100, 5e9, 5e11)])
    con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)",
                    [("000001", dd, 100 * 1.005 ** i) for i, dd in enumerate(days)] + [("000002", dd, 100.0) for dd in days])
    db.set_meta(con, "computed_date", obs)
    ctx = strategy.Ctx(con)
    st = {}
    r = strategy.etf_leader(ctx, st)
    assert r["gate"]["on"] is True
    assert [c["code"] for c in r["candidates"]] == ["000001"]
    assert len(r["opened_now"]) == 1 and r["opened_now"][0]["status"] == "pending"
    assert r["targets"]["000001"] == 0.4 and r["targets"]["153130"] == pytest.approx(7.6)
    # 같은 관측일을 다시 돌려도 중복 진입하지 않는다
    r2 = strategy.etf_leader(ctx, st)
    assert len(r2["open"]) == 1 and not r2["opened_now"]
    # 게이트 꺼짐(코스닥 120일선 아래)이면 신규 없음
    con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", [("KQ11", dd, 100 - i * 0.1) for i, dd in enumerate(days)])
    db.set_meta(con, "computed_date", obs)
    r3 = strategy.etf_leader(strategy.Ctx(con), {})
    assert r3["gate"]["on"] is False and not r3["opened_now"]


def test_compute_saves_state_only_when_all_sleeves_ok(con):
    days = tdays(300)
    seed_index(con, days, [100.0] * 300)
    seed_series(con, days, spx=[100.0] * 300, gold_usd=[100.0] * 300, usdkrw=[1300.0] * 300, vix=[15.0] * 300)
    plan = strategy.compute(con)
    assert plan["failed"] == []
    st = json.loads(db.get_meta(con, "plan_state"))
    assert st["last"]["date"] == plan["asof"] and abs(sum(st["last"]["targets"].values()) - 100) < 0.05
    assert strategy.latest(con)["asof"] == plan["asof"]
    assert con.execute("SELECT COUNT(*) FROM plan_log").fetchone()[0] == 1
