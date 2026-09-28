"""연구(research/)에서 5년치 과거 자료로 검증을 통과한 규칙만 레이더에 붙인다.

검증 방법·수치는 research/ 의 결과 파일이 정본이다(radar_defs.py · robustness.py · backtest.py · index_event_study.py).
통과 기준: 비슷한 종목(같은 날·시총·변동성·60일 모멘텀) 대비 우위가 두 기간에서 같은 방향, 날짜 고정효과 회귀(120일 수익률까지
통제, 날짜·종목 이중 군집)에서 남을 것, 그리고 **표본 밖 재현** — KODEX 로 찾은 규칙이 TIGER·PLUS·KIWOOM 매수(규칙을 정할 때
안 본 자료, research/replicate.py)에서도 같은 방향일 것. (2026-09-25 최종, KODEX·TIGER·PLUS·KIWOOM 142개 ETF)
  - 테마·중소형 주도주: KODEX +5.0%p → 다른 운용사만 폭등 1.17배·+5.9%p, 상위 10종목 빼도 1.07배 → 재현됨, 가장 큰 가산
  - 액티브+주도주: KODEX 1.24배 → 다른 운용사 1.11배로 약해짐 → 작은 가산
  - 주도주·신고가 근처: 다른 운용사 1.19배로 재현되지만 6개월 전 신호(위약)도 비슷 — 모멘텀 효과 → 작은 가산
  - 액티브 매수 자체: KODEX 1.12배였지만 다른 운용사 1.04배(t 0.5) → 재현 안 됨, 가산 없음
  - 코스닥150 편입 직후: 중앙값도 −8%, 11회 중 9회 마이너스 — 넓게 나타나는 약세 → 경고·감점
통과하지 못한 것 — 'ETF 가 몇 곳 샀나', '얼마나 많이 샀나' — 는 폭등 확률과 관계가 없었다(확신도에서 비중을 줄였다).
모든 매수 신호에서 '평균' 초과수익은 소수의 크게 오른 종목이 만든다(중앙값은 대조군보다 약간 낮다) — 복권형이라 손절이 필요하다.

가격 이력: px_hist(수정주가, 신호 종목만 FinanceDataReader 로 한 번 채움) 위에 stock_daily(매일 스냅샷 종가)를 덧댄다.
"""
from __future__ import annotations

import logging
import time
from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("edges")
KST = ZoneInfo("Asia/Seoul")

NOT_THEME = {"시장대표", "기타", "배당·가치", "리츠·부동산"}  # 연구의 '레이더 테마 ETF' 정의와 같다
INDEX_Q150 = {"229200", "232080"}  # KODEX·TIGER 코스닥150 — 신규편입이 곧 지수 편입
LEAD = 0.20        # 60거래일 수익률 − 시장지수 60일 수익률
NEAR_HIGH = 0.90   # 종가 ÷ 250거래일 최고 종가
SMALL_CAP_EOK = 10000  # 1조
Q150_WARN_DAYS = 90    # 편입 후 약 60거래일
BREAK = (0.66, 1.34)   # 하루 등락 제한 ±30% 밖의 비율 = 액면분할·병합 등으로 가격 단위가 바뀐 것

# 화면 문구. 수치는 research/cache/radar_defs.json · index_study.json (2021-01 ~ 2026-06, KODEX 81개 기준)
EDGE_INFO = {
    "theme_small_lead": {"t": "테마·중소형 주도주", "lv": "good",
                         "note": "테마 ETF가 산 시총 1조 미만 주도주 — 4개 운용사 과거 560건: 60일 안에 +30% 간 확률이 비슷한 종목의 "
                                 "1.18배, 60일 수익 +6.2%p. KODEX로 찾은 규칙이 TIGER·PLUS·KIWOOM 매수에서도 같게 나왔다(+5.9%p). "
                                 "다만 평균은 크게 오른 몇 종목이 만들고 절반 이상은 비슷한 종목보다 못했다 — 손절 기준을 정해 둘 것."},
    "active_lead": {"t": "액티브+주도주", "lv": "info",
                    "note": "액티브 ETF가 산 주도주 — 4개 운용사 과거 2,039건: 폭등 확률 1.17배. 다만 KODEX 밖(TIGER·PLUS·KIWOOM)에서는 "
                            "1.11배로 약해졌고, 액티브 매수 자체는 다른 운용사에서 효과가 없었다(1.04배). 참고로."},
    "lead_high": {"t": "주도주·신고가 근처", "lv": "info",
                  "note": "ETF가 산 주도주가 52주 고가 근처 — 4개 운용사 과거 2,365건: 폭등 확률 1.17배, 다른 운용사에서도 재현. "
                          "다만 같은 종목의 6개월 전 신호도 비슷하게 좋았다 — ETF 매수보다 '원래 강한 종목'이라서. 추세 확인용."},
    "q150_after": {"t": "코스닥150 편입 직후", "lv": "warn",
                   "note": "코스닥150 신규편입 종목은 편입일 전에 이미 오르고(평균 +6%), 편입일 이후 60일은 "
                           "지수보다 평균 −7% (이긴 비율 31%, 두 기간 모두 같음). 편입일 이후 새로 사는 것은 불리."},
}


def judge(net: float, cap_eok: float, rel60: float | None, hi52: float | None,
          theme_buy: bool, active_buy: bool, q150_recent: bool) -> list[str]:
    """검증된 규칙 중 이 종목에 해당하는 것(EDGE_INFO 키). 가격 이력이 모자라면(None) 주도주 규칙은 건너뛴다."""
    out = []
    lead = rel60 is not None and rel60 > LEAD
    if net > 0 and lead:
        if theme_buy and 0 < cap_eok < SMALL_CAP_EOK:
            out.append("theme_small_lead")
        if active_buy:
            out.append("active_lead")
        if hi52 is not None and hi52 >= NEAR_HIGH and not out:
            out.append("lead_high")  # 위 둘에 이미 걸리면 중복 표시하지 않는다
    if q150_recent:
        out.append("q150_after")
    return out


def conviction(n_moves: int, strength: float, n_act: int, active_buy: bool, edges: list[str], net: float) -> int:
    """확신도 0~100.
    매수: 움직인 곳 수(곳당 10, 최대 35) + 평소 거래 대비 규모(1배=35, 최대 35) + 검증된 근거(최대 30) − 경고.
      연구에서 '몇 곳이 샀나'·'얼마나 샀나'는 폭등 확률과 관계가 없어 예전(40+40)보다 비중을 낮췄다.
      근거(표본 밖 재현 기준): 테마·중소형 주도주 +15 · 액티브+주도주 +10 · 주도주·신고가 +5 · 액티브 매수 자체 0.
    매도: 예전 식 그대로(곳당 10 최대 40 + 규모 최대 40 + 액티브 곳당 10 최대 20)."""
    if not n_moves:
        return 0
    if net <= 0:
        return round(min(40, 10 * n_moves) + min(40, 40 * strength) + min(20, 10 * n_act))
    ev = (15 if "theme_small_lead" in edges else 0) + (10 if "active_lead" in edges else 0) + (5 if "lead_high" in edges else 0)
    pen = 25 if "q150_after" in edges else 0
    return max(0, min(100, round(min(35, 10 * n_moves) + min(35, 35 * strength) + min(30, ev) - pen)))


# ── 가격 이력 ──────────────────────────────────────────────────────────────

def closes(con, code: str, n: int = 260) -> list[tuple[str, float]]:
    """(날짜, 종가) 오래된 것부터. px_hist 를 바탕으로, 그 뒤 날짜는 stock_daily 스냅샷으로 잇는다."""
    rows = {r[0]: r[1] for r in con.execute(
        "SELECT date, close FROM px_hist WHERE code=? AND close>0 ORDER BY date DESC LIMIT ?", (code, n))}
    for d, c in con.execute("SELECT date, close FROM stock_daily WHERE code=? AND close>0 ORDER BY date DESC LIMIT ?", (code, n)):
        rows.setdefault(d, c)
    return sorted(rows.items())[-n:]


def features(px: list[tuple[str, float]], idx: list[tuple[str, float]]) -> tuple[float | None, float | None]:
    """(시장 대비 60거래일 수익률, 52주 고가 대비). 이력이 모자라면 None.
    연구와 같게: 60일 = 61개 종가의 처음과 끝, 52주 고가 = 최근 250개 종가의 최고(120개 미만이면 계산하지 않는다 — 연구의 min_periods=120)."""
    rel60 = hi52 = None
    if has_break(px[-250:]):
        return None, None  # 수정주가(px_hist)와 스냅샷(stock_daily) 단위가 어긋남 — 틀린 숫자보다 빈칸이 낫다
    if len(px) >= 61 and idx:
        d0, c0 = px[-61]
        d1, c1 = px[-1]
        i0, i1 = _at(idx, d0), _at(idx, d1)
        if i0 and i1:
            rel60 = (c1 / c0 - 1) - (i1 / i0 - 1)
    if len(px) >= 120:
        hi52 = px[-1][1] / max(c for _, c in px[-250:])
    return rel60, hi52


def has_break(px: list[tuple[str, float]]) -> bool:
    return any(not BREAK[0] <= b / a <= BREAK[1] for (_, a), (_, b) in zip(px, px[1:]))


def _at(series: list[tuple[str, float]], d: str) -> float | None:
    """d 이전(포함) 가장 가까운 값. 지수 자료가 며칠 늦게 올라올 때가 있다."""
    best = None
    for sd, v in series:
        if sd > d:
            break
        best = v
    return best


def q150_recent(con, asof: str) -> set[str]:
    since = (Date.fromisoformat(asof) - timedelta(days=Q150_WARN_DAYS)).isoformat()
    q = f"SELECT DISTINCT stock_code FROM change WHERE kind='NEW' AND date>? AND date<=? AND etf_code IN ({','.join('?' * len(INDEX_Q150))})"
    return {r[0] for r in con.execute(q, (since, asof, *sorted(INDEX_Q150)))}


INDEX_DAILY_URL = "https://m.stock.naver.com/api/index/{name}/price?pageSize=60&page={page}"


def index_history(name: str, pages: int) -> list[tuple[str, float]]:
    """네이버 지수 일별 종가(최근부터 60일씩). name = KOSPI | KOSDAQ."""
    import requests
    out = []
    for page in range(1, pages + 1):
        r = requests.get(INDEX_DAILY_URL.format(name=name, page=page), timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"})
        r.raise_for_status()
        rows = r.json()
        out += [(x["localTradedAt"], float(str(x["closePrice"]).replace(",", ""))) for x in rows if x.get("closePrice")]
        if len(rows) < 60:
            break
        time.sleep(0.3)
    if not out:
        raise ValueError(f"{name} 지수 일별 시세가 비었습니다")
    return out


def backfill(codes: list[str], days: int = 400) -> int:
    """신호 종목 중 아직 한 번도 받지 않은 것만 채운다(그 뒤 날짜는 매일 스냅샷 stock_daily 가 잇는다).
    상장한 지 얼마 안 된 종목은 받아도 이력이 짧다 — 다시 받지 않는다. 시장 지수는 매번 최근분을 덧붙인다."""
    import FinanceDataReader as fdr

    from . import db
    start = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.session() as con:
        last = {r[0]: r[1] for r in con.execute("SELECT code, MAX(date) FROM px_hist GROUP BY code")}
        # 받은 뒤 액면분할 등이 있었으면 저장된 이력의 단위가 오늘 종가와 달라진다 — 지우고 다시 받는다
        stale = [c for c in dict.fromkeys(codes) if c in last and has_break(closes(con, c)[-250:])]
        for c in stale:
            con.execute("DELETE FROM px_hist WHERE code=?", (c,))
            last.pop(c)
    if stale:
        log.info("가격 단위가 바뀐 종목 다시 받음: %s", ", ".join(stale))
    todo = [(c, start) for c in dict.fromkeys(codes) if c not in last]
    n = 0
    # 지수는 FinanceDataReader 가 며칠씩 늦게 끝나(2026-09 확인: 4거래일) 네이버 일별 시세로 받는다
    for code, name in (("KS11", "KOSPI"), ("KQ11", "KOSDAQ")):
        try:
            rows = index_history(name, pages=1 if code in last else 5)
        except Exception as ex:  # noqa: BLE001
            log.warning("지수 이력 실패 %s: %s", code, ex)
            continue
        with db.session() as con:
            con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", [(code, d, c) for d, c in rows])
        n += 1
    for code, st in todo:
        try:
            df = fdr.DataReader(code, st)
        except Exception as ex:  # noqa: BLE001 — 한 종목 실패로 전체를 멈추지 않는다
            log.warning("가격 이력 실패 %s: %s", code, ex)
            continue
        rows = [(code, d.strftime("%Y-%m-%d"), float(r.Close)) for d, r in df.iterrows() if r.Close and r.Close > 0]
        with db.session() as con:
            con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", rows)
        n += 1
        time.sleep(0.2)  # 비공식 경로에 부담을 주지 않는다
    return n


# ── 실전 성적 기록 ────────────────────────────────────────────────────────
# 과거 통계는 과최적화됐을 수 있다(여러 규칙을 시험했다). 레이더가 실제로 띄운 신호를 날마다 남기고,
# 20·60거래일 뒤 시장 지수 대비 성적을 근거별로 모아 본다 — 1~2년 쌓이면 규칙을 계속 쓸지 판단할 근거가 된다.
TRACK_GROUPS = [("all_up", "매수 신호 전체"), ("conv70", "확신도 70↑"), ("active_lead", "액티브+주도주"),
                ("theme_small_lead", "테마·중소형 주도주"), ("lead_high", "주도주·신고가 근처"),
                ("q150_after", "코스닥150 편입 직후"), ("all_down", "매도 신호 전체")]
HORIZONS = (20, 60)


def log_signals(con, b: dict) -> int:
    """bootstrap 묶음의 오늘 신호를 signal_log 에 남긴다(같은 날 다시 돌면 덮어쓴다)."""
    d = b.get("asof")
    if not d:
        return 0
    rows = []
    for side in ("up", "down"):
        for c in b["signals"][side]:
            s = b["stocks"][c]
            px = s.get("px") or []
            rows.append((d, c, side, s["kind"], s["conv"], ",".join(e["k"] for e in s.get("edges", [])),
                         px[-1] if px else None, s["cap"]))
    con.execute("DELETE FROM signal_log WHERE date=?", (d,))
    con.executemany("INSERT INTO signal_log(date,code,side,kind,conv,edges,close,cap) VALUES(?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def track(con, market: dict[str, str]) -> dict:
    """근거별 실전 성적: 신호일 종가 → h 거래일 뒤 종가, 같은 기간 시장 지수를 뺀 값. h 거래일이 아직 안 지난 신호는 뺀다."""
    idx = {m: closes(con, m, 5000) for m in ("KS11", "KQ11")}
    days = [d for d, _ in idx["KQ11"]]
    out = {h: {k: [] for k, _ in TRACK_GROUPS} for h in HORIZONS}
    first, cache = None, {}
    for date, code, side, conv, eds, close in con.execute(
            "SELECT date, code, side, conv, edges, close FROM signal_log WHERE close>0 ORDER BY date"):
        first = first or date
        i0 = _pos(days, date)
        if i0 is None:
            continue
        for h in HORIZONS:
            if i0 + h >= len(days):
                continue
            if code not in cache:
                cache[code] = closes(con, code, 5000)  # 신호일 뒤 h일 종가가 필요하다 — 최근 N일로 자르면 오래된 신호가 빠진다
            d1 = days[i0 + h]
            c1 = _at(cache[code], d1)
            ix = idx["KS11" if market.get(code) == "KOSPI" else "KQ11"]
            b0, b1 = _at(ix, date), _at(ix, d1)
            if not c1 or not b0 or not b1:
                continue
            x = (c1 / close - 1) - (b1 / b0 - 1)
            keys = ["all_up" if side == "up" else "all_down"] + [k for k in (eds or "").split(",") if k]
            if side == "up" and (conv or 0) >= 70:
                keys.append("conv70")
            for k in keys:
                if k in out[h]:
                    out[h][k].append(x)
    res = {"since": first, "groups": []}
    for k, label in TRACK_GROUPS:
        g = {"k": k, "t": label}
        for h in HORIZONS:
            v = sorted(out[h][k])
            g[f"n{h}"] = len(v)
            if v:
                g[f"mean{h}"] = round(sum(v) / len(v) * 100, 1)
                g[f"med{h}"] = round(v[len(v) // 2] * 100, 1)
                g[f"win{h}"] = round(sum(x > 0 for x in v) / len(v) * 100)
        res["groups"].append(g)
    return res


def _pos(days: list[str], d: str) -> int | None:
    """d 이전(포함) 마지막 거래일의 위치."""
    import bisect
    i = bisect.bisect_right(days, d) - 1
    return i if i >= 0 else None
