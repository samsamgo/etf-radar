from app import scoring as s
from app.themes import theme_of


def h(shares, weight):
    return {"shares": shares, "weight": weight}


def test_price_move_alone_is_not_a_signal():
    # 주가가 올라 비중만 10% → 14% 로 변했고 주식수는 그대로 → 시그널 아님
    prev = {"A": h(100, 10.0), "B": h(50, 90.0)}
    cur = {"A": h(100, 14.0), "B": h(50, 86.0)}
    assert s.classify_changes(prev, cur) == []


def test_new_exit_up_down():
    prev = {"A": h(100, 10), "B": h(50, 20), "C": h(10, 5), "T": h(1, 0.1)}
    cur = {"A": h(120, 12), "B": h(40, 16), "D": h(30, 6), "U": h(1, 0.1)}
    got = {c["name"]: c["kind"] for c in s.classify_changes(prev, cur)}
    assert got == {"A": s.UP, "B": s.DOWN, "C": s.EXIT, "D": s.NEW}  # 비중 0.3% 미만(T,U)은 무시


def test_whole_basket_rescale_is_ignored():
    prev = {n: h(100 * (i + 1), 10) for i, n in enumerate("ABCDEF")}
    cur = {n: h(v["shares"] * 2, 10) for n, v in prev.items()}
    assert s.classify_changes(prev, cur) is None


def test_real_rebalance_is_not_mistaken_for_rescale():
    prev = {n: h(100, 10) for n in "ABCDEF"}
    cur = dict(zip("ABCDEF", [h(130, 10), h(70, 10), h(115, 10), h(90, 10), h(160, 10), h(60, 10)]))
    assert len(s.classify_changes(prev, cur)) == 6


def test_est_amount_sign_and_size():
    up = {"kind": s.UP, "pct": 0.25, "w_prev": 8, "w_cur": 10}
    # 순자산 1000억, 비중 10% = 100억 보유. 주식수 25% 증가 → 그중 20억어치가 새로 산 것
    assert round(s.est_amount(up, 1000e8, 1000e8) / 1e8) == 20
    assert s.est_amount({"kind": s.EXIT, "pct": None, "w_prev": 5, "w_cur": 0}, 1000e8, 900e8) == -50e8
    assert s.est_amount({"kind": s.NEW, "pct": None, "w_prev": 0, "w_cur": 4}, 1000e8, 900e8) == 36e8
    assert s.est_amount({"kind": s.DOWN, "pct": -0.5, "w_prev": 10, "w_cur": 5}, 1000e8, 1000e8) == -50e8


def test_large_cap_sinks_in_impact_score():
    A = {"samsung": 0.02, "small": 0.10, "mid": 0.06}
    B = {"samsung": 0.5, "small": 8.0, "mid": 4.0}
    C = {"samsung": 0.3, "small": 0.7, "mid": 0.5}
    sc = s.impact_scores(A, B, C)
    assert sc["small"] == 100 and sc["samsung"] == 0 and 0 < sc["mid"] < 100


def test_pick_hot_excludes_leveraged_and_small():
    etfs = [
        {"code": "1", "name": "K방산", "tab": 2, "equity_w": 99, "ret3m": 40, "aum_eok": 3000},
        {"code": "2", "name": "K방산 레버리지", "tab": 2, "equity_w": 99, "ret3m": 90, "aum_eok": 3000},
        {"code": "3", "name": "소형테마", "tab": 2, "equity_w": 99, "ret3m": 50, "aum_eok": 100},
        {"code": "4", "name": "미국테크", "tab": 4, "equity_w": 0, "ret3m": 60, "aum_eok": 9000},
        {"code": "5", "name": "코스피", "tab": 1, "equity_w": 99, "ret3m": 5, "aum_eok": 9000},
        {"code": "6", "name": "은행", "tab": 2, "equity_w": 99, "ret3m": 1, "aum_eok": 9000},
        # 하락장에서는 채권 ETF가 '가장 덜 빠진 ETF'가 된다 — 수익률이 1등이어도 뽑히면 안 된다
        {"code": "7", "name": "26-12 은행채(AA+이상)액티브", "tab": 2, "equity_w": 0, "ret3m": 95, "aum_eok": 9000},
    ]
    assert s.pick_hot(etfs, 500, 0.5) == {"1"}


def test_source_classification_is_not_trusted():
    # 2026-09-21 실측: 자료원은 아래 ETF를 전부 '국내주식형'으로 분류해 내려준다. 분류를 믿으면 채권 ETF가 핫 ETF가 된다.
    for name in ("HANARO 26-12 은행채(AA+이상)액티브", "KODEX 인도Nifty미드캡100", "PLUS 미국배당증가성장주데일리커버드콜"):
        assert not s.holds_domestic_stocks({"name": name, "tab": 2, "type": "국내주식형, 전략지수", "equity_w": 0.0})
    assert s.holds_domestic_stocks({"name": "KODEX 반도체", "tab": 2, "type": "국내주식형, 섹터", "equity_w": 99.8})
    assert s.holds_domestic_stocks({"name": "KODEX 200커버드콜액티브", "tab": 1, "equity_w": 84.2})  # 옵션·현금이 섞여도 주식형


def test_rebuild_weights_when_source_gives_all_zero():
    # 실측: KODEX 현대차로보틱스밸류체인TOP3플러스 — 주식수는 오는데 비중이 전부 0.0 (보유 10행 중 8행이 상장 주식)
    rows = [{"name": "기아", "shares": 600, "weight": 0.0, "close": 100_000},
            {"name": "현대차", "shares": 100, "weight": 0.0, "close": 200_000},
            {"name": "현대모비스", "shares": 50, "weight": 0.0, "close": 300_000},
            {"name": "HL만도", "shares": 100, "weight": 0.0, "close": 50_000},
            {"name": "NVIDIA Corp", "shares": 27, "weight": 0.0, "close": None},   # 해외 종목: 값을 모른다
            {"name": "원화현금", "shares": 0, "weight": 0.0, "close": None}]      # 주식수 0 — 보유 행으로 세지 않는다
    got = {r["name"]: r["weight"] for r in s.rebuild_weights(rows)}
    assert got == {"기아": 60.0, "현대차": 20.0, "현대모비스": 15.0, "HL만도": 5.0, "NVIDIA Corp": 0.0, "원화현금": 0.0}


def test_rebuild_weights_refuses_when_most_rows_are_unknown():
    # 실측: 전 세계 1,561종목을 담은 ETF가 '삼성전자 35.3%'로, 채권혼합 ETF가 '삼성전자 100%'로 복원된 적이 있다
    world = [{"name": "삼성전자", "shares": 50, "weight": 0.0, "close": 270_000}] + \
            [{"name": f"FOREIGN {i}", "shares": 10, "weight": 0.0, "close": None} for i in range(40)]
    mixed = [{"name": "삼성전자", "shares": 80, "weight": 0.0, "close": 270_000},
             {"name": "국고채 03-27", "shares": 500, "weight": 0.0, "close": None},
             {"name": "통안채", "shares": 300, "weight": 0.0, "close": None}]
    assert s.rebuild_weights(world) is None
    assert s.rebuild_weights(mixed) is None


def test_rebuild_weights_leaves_normal_etf_alone():
    assert s.rebuild_weights([{"name": "A", "shares": 10, "weight": 55.0, "close": 1000}]) is None
    assert s.rebuild_weights([{"name": "국고채", "shares": 10, "weight": 0.0, "close": None}]) is None  # 복원할 근거가 없다


def test_bond_mixed_etf_is_not_equity():
    # 실제로 'KODEX 삼성전자채권혼합'이 국내 주식 분류(tab 1·2)로 들어와 핫 ETF에 뽑힌 적이 있다
    assert not s.is_plain_equity({"name": "KODEX 삼성전자채권혼합", "tab": 2})
    assert not s.is_plain_equity({"name": "TIGER 200선물레버리지", "tab": 1})
    assert s.is_plain_equity({"name": "KODEX 반도체", "tab": 2})
    assert s.is_plain_equity({"name": "TIGER 배당커버드콜액티브", "tab": 2})  # 커버드콜은 주식을 실제로 든다


def test_streak():
    assert s.streak([-1, 2, 3, 1]) == 3
    assert s.streak([2, -1, -4]) == -2
    assert s.streak([1, 0]) == 0
    assert s.streak([]) == 0


def test_theme_of():
    assert theme_of("x", "KODEX 반도체") == "반도체"
    assert theme_of("x", "TIGER 2차전지테마") == "2차전지"
    assert theme_of("x", "SOL 조선TOP3플러스") == "조선"
    assert theme_of("x", "KODEX 200") == "시장대표"
    # '인프라'가 들어간 두 이름은 서로 다른 테마다 — 실제로 리츠 ETF가 전력설비로 묶였던 적이 있다
    assert theme_of("x", "TIGER 리츠부동산인프라") == "리츠·부동산"
    assert theme_of("x", "TIGER 전력인프라") == "전력설비"
