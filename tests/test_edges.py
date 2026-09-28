from app import edges as e


def series(vals, start=0):
    return [(f"2026-{1 + (i + start) // 28:02d}-{1 + (i + start) % 28:02d}", v) for i, v in enumerate(vals)]


def test_features_match_research_definition():
    # 61개 종가: 100 → 150 (+50%), 같은 기간 지수 +10% → 시장 대비 +40%p
    px = series([100.0] * 200 + [100 + i * 50 / 60 for i in range(61)])
    idx = series([1000.0] * 200 + [1000 + i * 100 / 60 for i in range(61)])
    rel60, hi52 = e.features(px, idx)
    assert abs(rel60 - 0.40) < 1e-9
    assert hi52 == 1.0  # 마지막 종가가 최고가


def test_features_need_enough_history():
    px = series([100.0] * 50)
    assert e.features(px, series([1.0] * 50)) == (None, None)
    rel60, hi52 = e.features(series([100.0] * 100), series([1.0] * 100))
    assert rel60 == 0 and hi52 is None  # 52주 고가는 120개 이상일 때만


def test_index_lag_uses_last_known_value():
    # 지수 자료가 종목보다 며칠 늦게 끝나도 가장 가까운 이전 값으로 맞춘다
    assert e._at([("2026-01-01", 1.0), ("2026-01-05", 2.0)], "2026-01-09") == 2.0
    assert e._at([("2026-01-05", 2.0)], "2026-01-01") is None


def test_judge_rules():
    kw = dict(theme_buy=True, active_buy=False, q150_recent=False)
    assert e.judge(1e9, 5000, 0.25, 0.95, **kw) == ["theme_small_lead"]
    assert e.judge(1e9, 15000, 0.25, 0.95, **kw) == ["lead_high"]          # 1조 이상 → 중소형 아님
    assert e.judge(1e9, 5000, 0.15, 0.95, **kw) == []                      # 주도주 아님
    assert e.judge(-1e9, 5000, 0.25, 0.95, **kw) == []                     # 판 종목엔 매수 근거를 붙이지 않는다
    assert e.judge(1e9, 5000, None, None, **kw) == []                      # 이력이 모자라면 건너뛴다
    assert e.judge(1e9, 0, 0.25, 0.5, **kw) == []                          # 시총 모름 → 중소형 판정 안 함
    both = e.judge(1e9, 5000, 0.25, 0.95, theme_buy=True, active_buy=True, q150_recent=True)
    assert both == ["theme_small_lead", "active_lead", "q150_after"]       # 신고가 근처는 위 둘과 겹치면 빼고, 경고는 늘 붙인다
    assert e.judge(-1e9, 5000, None, None, theme_buy=False, active_buy=False, q150_recent=True) == ["q150_after"]


def test_conviction():
    # 매수: 2곳(20) + 규모 0.5배(17.5) = 37.5 → 38, 근거 없음
    assert e.conviction(2, 0.5, 0, False, [], 1e9) == 38
    # 테마·중소형(+15) + 액티브+주도주(+10) = 25. 액티브 매수 자체는 가산 없음(다른 운용사에서 재현 안 됨)
    assert e.conviction(2, 0.5, 1, True, ["theme_small_lead", "active_lead"], 1e9) == 62  # 62.5 → 짝수 반올림
    assert e.conviction(2, 0.5, 1, True, [], 1e9) == 38
    assert e.conviction(2, 0.5, 0, False, ["lead_high"], 1e9) == 42    # 신고가 근처 +5 (42.5 → 42)
    # 코스닥150 편입 직후 경고 −25
    assert e.conviction(2, 0.5, 0, False, ["q150_after"], 1e9) == 12
    assert e.conviction(1, 0, 0, False, ["q150_after"], 1e9) == 0      # 0 아래로 내려가지 않는다
    assert e.conviction(9, 9, 5, True, ["theme_small_lead", "active_lead"], 1e9) == 95  # 35 + 35 + 근거 25
    # 매도: 예전 식 그대로
    assert e.conviction(2, 0.5, 1, False, [], -1e9) == 20 + 20 + 10
    assert e.conviction(0, 0, 0, False, [], 0) == 0


def test_edge_info_complete():
    for k, v in e.EDGE_INFO.items():
        assert v["t"] and v["note"] and v["lv"] in ("good", "info", "warn"), k


def test_split_break_blanks_features():
    # 과거 이력은 분할 전 단위(10만 원대), 최근 스냅샷은 분할 후(2만 원대) → 계산하지 않는다
    px = series([100000.0] * 150 + [20000.0] * 20)
    assert e.has_break(px)
    assert e.features(px, series([1.0] * 170)) == (None, None)
    # 상한가(+30%)는 정상 움직임
    assert not e.has_break(series([100.0, 130.0, 91.0]))


def test_track_forward_excess_return():
    import sqlite3
    from app import db
    con = sqlite3.connect(":memory:")
    con.executescript(db.SCHEMA)
    days = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(80)]
    # 지수는 그대로, 종목 A 는 20거래일 뒤 +10%, 종목 B 는 −5%
    con.executemany("INSERT INTO px_hist VALUES('KQ11',?,1000)", [(d,) for d in days])
    con.executemany("INSERT INTO px_hist VALUES('A',?,?)", [(d, 100 if i < 20 else 110) for i, d in enumerate(days)])
    con.executemany("INSERT INTO px_hist VALUES('B',?,?)", [(d, 100 if i < 20 else 95) for i, d in enumerate(days)])
    con.execute("INSERT INTO signal_log VALUES(?, 'A', 'up', 'UP', 80, 'active_lead', 100, 5000)", (days[0],))
    con.execute("INSERT INTO signal_log VALUES(?, 'B', 'up', 'UP', 40, '', 100, 5000)", (days[0],))
    con.execute("INSERT INTO signal_log VALUES(?, 'A', 'up', 'UP', 80, 'active_lead', 110, 5000)", (days[70],))  # 20일이 안 지남
    t = {g["k"]: g for g in e.track(con, {})["groups"]}
    assert t["all_up"]["n20"] == 2 and t["all_up"]["mean20"] == 2.5 and t["all_up"]["win20"] == 50
    assert t["active_lead"]["n20"] == 1 and t["active_lead"]["mean20"] == 10.0
    assert t["conv70"]["n20"] == 1
    assert t["all_up"]["n60"] == 2  # 60거래일도 지남(80일치)
