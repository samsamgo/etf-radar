"""계산 로직 — DB·네트워크를 모르는 순수 함수만 둔다(테스트 대상)."""
from __future__ import annotations

from statistics import median, pstdev

NEW, EXIT, UP, DOWN = "NEW", "EXIT", "UP", "DOWN"


def is_rescale(prev: dict, cur: dict, tol: float) -> bool:
    """1CU 구성이 통째로 같은 비율로 바뀐 날(설정단위 변경·액면분할성 조정)은 매매가 아니다."""
    ratios = [cur[n]["shares"] / prev[n]["shares"] for n in prev.keys() & cur.keys() if prev[n]["shares"] > 0]
    if len(ratios) < 5:
        return False
    moved = [r for r in ratios if abs(r - 1) > tol]
    if len(moved) < 0.8 * len(ratios):
        return False
    m = median(moved)
    return m > 0 and pstdev(moved) / m < 0.02


def classify_changes(prev: dict, cur: dict, tol: float = 0.005, min_weight: float = 0.3):
    """전일·당일 1CU당 주식수를 비교해 신규편입/편출/확대/축소를 가른다.

    비중이 아니라 주식수로 본다 — 비중은 주가만 올라도 변하지만 주식수는 실제로 사고팔아야 변한다.
    prev/cur: {종목명: {"shares": float, "weight": float}}.  통째 조정일이면 None.
    """
    if not prev or not cur:
        return []
    if is_rescale(prev, cur, tol):
        return None
    out = []
    for name in prev.keys() | cur.keys():
        p, c = prev.get(name), cur.get(name)
        if p is None:
            if c["weight"] >= min_weight:
                out.append({"name": name, "kind": NEW, "pct": None, "w_prev": 0.0, "w_cur": c["weight"]})
        elif c is None:
            if p["weight"] >= min_weight:
                out.append({"name": name, "kind": EXIT, "pct": None, "w_prev": p["weight"], "w_cur": 0.0})
        elif p["shares"] > 0:
            pct = c["shares"] / p["shares"] - 1
            if abs(pct) > tol:
                out.append({"name": name, "kind": UP if pct > 0 else DOWN, "pct": pct,
                            "w_prev": p["weight"], "w_cur": c["weight"]})
    return out


def est_amount(change: dict, aum_prev: float, aum_cur: float) -> float:
    """그 변화가 대략 얼마어치 매매였는지(원). 매수는 +, 매도는 -."""
    k = change["kind"]
    if k == NEW:
        return aum_cur * change["w_cur"] / 100
    if k == EXIT:
        return -aum_prev * change["w_prev"] / 100
    held = aum_cur * change["w_cur"] / 100
    pct = change["pct"]
    return held * pct / (1 + pct)  # 오늘 보유분 중 새로 산(또는 판) 몫


def pct_rank(values: dict) -> dict:
    """값 → 0~1 백분위. 동점은 같은 순위."""
    if not values:
        return {}
    order = sorted(set(values.values()))
    if len(order) == 1:
        return {k: 0.5 for k in values}
    pos = {v: i / (len(order) - 1) for i, v in enumerate(order)}
    return {k: pos[v] for k, v in values.items()}


def impact_scores(A: dict, B: dict, C: dict) -> dict:
    """영향도(0~100) = 지분 중 ETF 보유(A) 37.5 + 거래량 대비 물량(B) 37.5 + 핫 ETF 몫(C) 25."""
    a, b, c = pct_rank(A), pct_rank(B), pct_rank(C)
    return {k: round(100 * (0.375 * a[k] + 0.375 * b[k] + 0.25 * c[k])) for k in A}


LEVERAGED = ("레버리지", "인버스", "2X", "선물", "곱버스")
# 네이버 분류로는 국내 주식(1·2)에 들어 있어도 채권이 섞인 상품. 하락장에서 덜 빠져 '수익률 상위'로 올라오므로 반드시 거른다
NOT_EQUITY = ("혼합", "채권", "TDF", "TRF", "머니마켓", "금리", "KOFR")


def is_plain_equity(etf: dict) -> bool:
    """개별 종목을 실제로 사는 국내 주식형만."""
    name = etf["name"].upper()
    return etf["tab"] in (1, 2) and not any(w in name for w in LEVERAGED + NOT_EQUITY)


def holds_domestic_stocks(etf: dict, min_equity_w: float = 80) -> bool:
    """이 ETF가 국내 개별 종목을 실제로 사는가 — 구성종목 중 상장 주식으로 확인된 비중으로 판단한다.

    이름으로는 못 막는다('은행채' ETF가 주식 분류로 들어온 적이 있다).
    자료원의 상품 분류(etf.type)도 믿을 수 없다: 2026-09-21 실측에서 은행채·인도·미국배당·채권혼합 ETF가
    전부 '국내주식형'으로 내려왔다. 그래서 분류는 저장만 하고 판단에 쓰지 않는다.
    """
    return is_plain_equity(etf) and etf.get("equity_w", 0) >= min_equity_w


def rebuild_weights(rows: list[dict], min_known_rows: float = 0.7) -> list[dict] | None:
    """자료원이 비중을 전부 0으로 줄 때, 주식수×종가로 비중(%)을 복원한다. 복원할 수 없으면 None.

    rows: [{"name", "shares", "weight", "close"}] — close 는 상장 주식으로 확인된 종목만 있다.
    값을 모르는 종목(해외·채권·선물)은 분모에서 빠지므로, 그런 행이 많으면 복원값은 거짓이 된다
    (실측: 전 세계 1,561종목 ETF가 '삼성전자 35%'로 복원됐다). 그래서 보유 행의 대부분이 확인될 때만 복원한다.
    그래도 빠진 행만큼 실제보다 조금 크게 나오는 추정치다.
    """
    if not rows or sum(r["weight"] for r in rows) > 1:
        return None  # 비중이 정상적으로 온 ETF
    held = [r for r in rows if r["shares"] > 0]  # 주식수 0 인 '원화현금' 행은 세지 않는다
    if not held or sum(1 for r in held if r.get("close")) < min_known_rows * len(held):
        return None
    total = sum(r["shares"] * r["close"] for r in rows if r.get("close"))
    if total <= 0:
        return None
    return [{**r, "weight": round(100 * r["shares"] * r["close"] / total, 2) if r.get("close") else 0.0} for r in rows]


def pick_hot(etfs: list[dict], min_aum_eok: float = 500, top: float = 0.30) -> set:
    """순자산이 크면서 3개월 수익률이 국내 주식형 상위 top 안에 드는 ETF."""
    pool = [e for e in etfs if holds_domestic_stocks(e) and e.get("ret3m") is not None]
    if not pool:
        return set()
    rets = sorted((e["ret3m"] for e in pool), reverse=True)
    cut = rets[max(0, int(len(rets) * top) - 1)]
    return {e["code"] for e in pool if e["ret3m"] >= cut and e["aum_eok"] >= min_aum_eok}


def streak(net_by_day: list[float]) -> int:
    """최근일부터 같은 방향(순매수/순매도)이 며칠 이어졌나. +는 매집, -는 이탈."""
    n = 0
    for v in reversed(net_by_day):
        if v == 0 or (n and (v > 0) != (n > 0)):
            break
        n += 1 if v > 0 else -1
    return n
