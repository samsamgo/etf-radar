"""바깥 데이터. 전부 비공식 엔드포인트라 언제든 모양이 바뀔 수 있다 — 바뀌면 SourceError 로 크게 알린다."""
from __future__ import annotations

import json

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
ETF_LIST_URL = "https://finance.naver.com/api/sise/etfItemList.nhn"
ETF_PAGE_URL = "https://navercomp.wisereport.co.kr/v2/ETF/index.aspx?cmp_cd={code}"
REALTIME_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"


class SourceError(RuntimeError):
    pass


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def etf_list() -> list[dict]:
    """상장 ETF 전체: 현재가·NAV·순자산(억)·3개월 수익률. 한 번 호출."""
    r = requests.get(ETF_LIST_URL, headers=UA, timeout=20)
    r.raise_for_status()
    try:
        items = r.json()["result"]["etfItemList"]
    except (KeyError, ValueError) as e:
        raise SourceError(f"ETF 목록 형식이 바뀌었습니다: {e}") from e
    if len(items) < 100:
        raise SourceError(f"ETF 목록이 비정상적으로 적습니다: {len(items)}개")
    return [{"code": i["itemcode"], "name": i["itemname"], "tab": int(i["etfTabCode"]),
             "price": _num(i.get("nowVal")), "nav": _num(i.get("nav")), "chg": _num(i.get("changeRate")),
             "ret3m": _num(i.get("threeMonthEarnRate")), "aum_eok": _num(i.get("marketSum")) or 0.0,
             "amount": (_num(i.get("amonut")) or 0.0) * 1e6}  # 거래대금(원). 자료원의 철자가 amonut 이다
            for i in items]


def _js_var(html: str, name: str):
    at = html.find(f"var {name} =")
    if at < 0:
        return None
    start = html.index("=", at) + 1
    try:
        return json.JSONDecoder().raw_decode(html[start:].lstrip())[0]
    except ValueError:
        return None


def parse_etf_page(html: str) -> dict:
    cu = _js_var(html, "CU_data")
    if not cu or "grid_data" not in cu:
        raise SourceError("구성종목(CU_data)을 찾지 못했습니다")
    rows = [g for g in cu["grid_data"] if g.get("STK_NM_KOR")]
    summary = _js_var(html, "summary_data") or {}
    status = _js_var(html, "status_data") or {}
    return {
        "date": rows[0]["TRD_DT"] if rows else None,
        "holdings": [{"name": g["STK_NM_KOR"].strip(), "shares": float(g.get("AGMT_STK_CNT") or 0),
                      "weight": float(g.get("ETF_WEIGHT") or 0)} for g in rows],
        "base_index": summary.get("BASE_IDX_NM_KOR"), "issuer": summary.get("ISSUE_NM_KOR"),
        "type": summary.get("ETF_TYP_SVC_NM") or "",
        "units_k": _num(status.get("LIST_STK_CNT")),  # 상장좌수(천 좌)
    }


def etf_page(code: str) -> tuple[dict, str]:
    r = requests.get(ETF_PAGE_URL.format(code=code), headers=UA, timeout=20)
    r.raise_for_status()
    return parse_etf_page(r.text), r.text


def stock_listing() -> list[dict]:
    """전 종목 종가·거래대금·시가총액 스냅샷."""
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    need = {"Code", "Name", "Market", "Close", "Amount", "Marcap"}
    if not need <= set(df.columns) or len(df) < 1000:
        raise SourceError(f"종목 목록 형식이 바뀌었습니다: {list(df.columns)[:8]} / {len(df)}행")
    return [{"code": r.Code, "name": str(r.Name).strip(), "market": r.Market,
             "close": float(r.Close or 0), "amount": float(r.Amount or 0), "marcap": float(r.Marcap or 0)}
            for r in df.itertuples()]


def amount_history(code: str, start: str) -> list[tuple[str, float, float]]:
    """(날짜, 종가, 거래대금 근사=종가×거래량). 20일 평균을 처음부터 쓰기 위한 1회성 보충."""
    import FinanceDataReader as fdr
    df = fdr.DataReader(code, start)
    return [(d.strftime("%Y-%m-%d"), float(r.Close), float(r.Close) * float(r.Volume)) for d, r in df.iterrows()]


MARKET_TREND_URL = ("https://stock.naver.com/api/domestic/market/trend/time"
                    "?tradeType=KRX&marketType={m}&bizdate={d}&startIdx=0&pageSize=1")
MARKET_PROGRAM_URL = ("https://stock.naver.com/api/domestic/market/trendProgram"
                      "?tradeType=KRX&krxMarketType={m}&bizdate={d}&startIdx=0&pageSize=1&periodType=TIME")
INSTITUTIONS = ("1000", "2000", "3000", "3100", "4000", "5000", "6000")  # 금융투자·보험·투신·사모·은행·기타금융·연기금


_NAVER_STOCK = {**UA, "Referer": "https://stock.naver.com/"}
INDEX_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ"


def _flow_row(row: dict) -> dict:
    net = {a["investorGubun"]: _num(a.get("diffValue")) or 0.0 for a in row.get("netAmounts", [])}
    if not {"1000", "8000", "9000"} <= net.keys():
        raise SourceError(f"투자자 동향 형식이 바뀌었습니다: {sorted(net)}")
    return {"time": row.get("time"), "fin": net["1000"], "inst": sum(net.get(k, 0.0) for k in INSTITUTIONS),
            "foreign": net["9000"], "indiv": net["8000"]}


def market_flow_series(market: str, bizdate: str) -> list[dict]:
    """당일 누적 순매수의 분 단위 흐름(오래된 것부터). 하루에 한 번만 부른다 — 이후에는 market_flows 의 최신 1행을 덧붙인다."""
    out, idx = [], 0
    for _ in range(4):  # 한 번에 최대 200행, 하루 400행 남짓
        r = requests.get(MARKET_TREND_URL.format(m=market, d=bizdate).replace("startIdx=0&pageSize=1", f"startIdx={idx}&pageSize=200"),
                         headers=_NAVER_STOCK, timeout=15)
        r.raise_for_status()
        j = r.json()
        rows = j.get("content") or []
        out += [_flow_row(x) for x in rows]
        if j.get("last", True) or not rows:
            break
        idx += 1
    return sorted(out, key=lambda x: x["time"])


def index_quotes() -> dict:
    """코스피·코스닥 지수 현재값과 등락률."""
    r = requests.get(INDEX_URL, headers=UA, timeout=10)
    r.raise_for_status()
    out = {}
    for d in r.json().get("datas", []):
        sign = -1 if (d.get("compareToPreviousPrice") or {}).get("name") in ("FALLING", "LOWER_LIMIT") else 1
        out[d["itemCode"]] = {"price": _num(d.get("closePrice")), "chg": sign * abs(_num(d.get("fluctuationsRatio")) or 0.0)}
    return out


def market_flows(market: str, bizdate: str) -> dict | None:
    """시장 전체의 당일 누적 순매수(원): 금융투자·기관 합계·외국인·개인, 프로그램 차익/비차익. 최신 1분 봉만 받는다.

    종목별 장중 수급은 키 없이 받을 수 있는 곳이 없다(2026-09-21 조사) — 여기서 주는 것은 시장 단위뿐이다.
    의미가 확인된 분류만 쓴다. 7000·7100 은 정체가 검증되지 않아 내보내지 않는다.
    """
    r = requests.get(MARKET_TREND_URL.format(m=market, d=bizdate), headers=_NAVER_STOCK, timeout=10)
    r.raise_for_status()
    rows = r.json().get("content") or []
    if not rows:
        return None  # 휴장일이거나 장 시작 전
    out = _flow_row(rows[0])
    r = requests.get(MARKET_PROGRAM_URL.format(m=market, d=bizdate), headers=_NAVER_STOCK, timeout=10)
    r.raise_for_status()
    p = (r.json().get("content") or [{}])[0]
    out.update(arb=_num(p.get("diffPureBuyAmt")), nonarb=_num(p.get("biDiffPureBuyAmt")), prog=_num(p.get("totalDiffPureBuyAmt")))
    return out


def realtime(codes: list[str]) -> dict:
    """장중 현재가. {code: {price, chg, amount_txt, at}}, 그리고 'open' 여부."""
    out, is_open = {}, False
    for i in range(0, len(codes), 40):
        r = requests.get(REALTIME_URL.format(codes=",".join(codes[i:i + 40])), headers=UA, timeout=10)
        r.raise_for_status()
        for d in r.json().get("datas", []):
            is_open = is_open or d.get("marketStatus") == "OPEN"
            sign = -1 if (d.get("compareToPreviousPrice") or {}).get("name") in ("FALLING", "LOWER_LIMIT") else 1
            out[d["itemCode"]] = {"price": _num(d.get("closePrice")),
                                  "chg": sign * abs(_num(d.get("fluctuationsRatio")) or 0.0),
                                  "amount_txt": d.get("accumulatedTradingValue"), "at": d.get("localTradedAt")}
    return {"open": is_open, "quotes": out}
