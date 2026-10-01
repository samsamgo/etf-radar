from datetime import datetime

import pytest

from app import intraday as it


def pt(t, v=1.0):
    return {"time": t, "fin": v, "inst": v, "foreign": v, "indiv": v}


@pytest.fixture
def fake(monkeypatch):
    """네이버 호출을 모두 가짜로 바꾼다. flows[m] 이 None 이면 장 시작 직후처럼 수급이 아직 비어 있다."""
    box = {"now": datetime(2026, 10, 1, 9, 2, tzinfo=it.KST), "flows": {}, "series": {}}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return box["now"]

    monkeypatch.setattr(it, "datetime", Clock)
    monkeypatch.setattr(it.build, "cached", lambda: {"asof": None, "hotEtfs": []})
    monkeypatch.setattr(it.sources, "etf_list", lambda: [])
    monkeypatch.setattr(it.sources, "index_quotes", lambda: {})
    monkeypatch.setattr(it, "_themes_today", lambda listing: {})
    monkeypatch.setattr(it.config, "MARKET_FLOWS", True)
    monkeypatch.setattr(it.sources, "market_flows", lambda m, d: box["flows"].get(m))
    monkeypatch.setattr(it.sources, "market_flow_series", lambda m, d: list(box["series"].get(m, [])))
    yesterday = {m: [pt("090100"), pt("153000")] for m in it.MARKETS}
    monkeypatch.setattr(it, "_state", {**it._state, "day": "20260930", "series": yesterday, "market": {m: pt("153000") for m in it.MARKETS}})
    return box


def test_yesterday_line_is_not_carried_into_today(fake):
    it.tick()  # 09:02, 수급이 아직 비어 있다
    assert it._state["series"] == {} and it._state["market"] == {}

    fake["now"] = fake["now"].replace(minute=5)
    fake["flows"] = {m: pt("090400") for m in it.MARKETS}
    fake["series"] = {m: [pt("090100"), pt("090300")] for m in it.MARKETS}
    it.tick()
    for m in it.MARKETS:
        times = [p["time"] for p in it._state["series"][m]]
        assert times == ["090100", "090300", "090400"]  # 어제 153000 이 앞에 붙지 않는다


def test_same_day_line_survives_one_failed_market(fake):
    fake["flows"] = {m: pt("090400") for m in it.MARKETS}
    fake["series"] = {m: [pt("090100")] for m in it.MARKETS}
    it.tick()
    fake["flows"] = {"KOSPI": pt("090500")}  # 코스닥만 한 번 빈다
    it.tick()
    assert [p["time"] for p in it._state["series"]["KOSDAQ"]] == ["090100", "090400"]
    assert [p["time"] for p in it._state["series"]["KOSPI"]] == ["090100", "090400", "090500"]
