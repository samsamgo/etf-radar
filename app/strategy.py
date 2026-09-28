"""투자 계획(V3B) — 연구(research/lab, 2026-09-25 final_v3)에서 남은 규칙으로 매일 '다음 거래일 종가에 무엇을 얼마나' 를 계산한다.

구성(총자산 100%): 코어 70% = Faber 10개월선 3자산(KODEX200 · TIGER 미국S&P500(환노출) · ACE KRX금현물, 각 23.3%)
                 위성 30% = 저변동 모멘텀 10 · ETF추가 주도주 8 · 코스피 3일 급락 반전 6 · 대형 저PBR 6.
규칙 원문: research/lab/notes/final_v3.md §9 (한 장 규칙집), 갈래별 명세 final_v2.md §9. 연구의 live/sleeves.py 를 서버용으로 옮겼다.

원칙
- 모든 목표는 '신호일(asof) 종가로 계산 → 다음 거래일 종가 체결'. 장 마감 뒤 수집이 끝나면 한 번, 아침 수집 뒤 한 번 돈다(같은 신호일이면 결과가 같다).
- 자료: 지수(px_hist, 네이버) · 지수 ETF·S&P·금·환율·VIX(series, FinanceDataReader) · 전 종목 종가/거래대금/시총(stock_daily, 매일 스냅샷)
  · 종목 1년 이력(px_hist, 월말에 전 종목 보충) · PBR/EPS(fund, 네이버 종목 페이지, 월 1회) · ETF 매매(change).
- 상태(보유 중인 사건형 포지션, 월별 목록, 마지막 목표)는 meta 'plan_state' 에 JSON 으로 둔다. 갈래 하나라도 계산에 실패하면
  그 실행의 목표는 상태에 남기지 않는다(다음 실행의 diff 기준이 흔들리지 않게).
- 기대치는 연 8% 안팎·MDD −20% (7개 시장 1968~2026 중앙값). 2021~26 성적(+20%)은 사후 성적이다 — 화면에도 그렇게 쓴다.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, edges

log = logging.getLogger("strategy")
KST = ZoneInfo("Asia/Seoul")

W = {"core": 70.0, "mom": 10.0, "etf": 8.0, "crash": 6.0, "val": 6.0}   # 총자산 대비 %
CORE = {"KS": "069500", "SPX": "360750", "GOLD": "411060"}
CASH = "153130"
NAMES = {"069500": "KODEX 200", "360750": "TIGER 미국S&P500", "411060": "ACE KRX금현물", "153130": "KODEX 단기채권"}
FABER_MONTHS = 10
MOM_N, MOM_POOL, MOM_TOP_MCAP, MOM_MIN_AMT = 40, 200, 200, 10e8
ETF_SLOTS, ETF_HOLD, ETF_STOP, ETF_TAKE = 20, 60, 0.15, 0.30
CRASH_THR, CRASH_HOLD = -0.025, 3
VAL_N, VAL_MIN_MCAP, VAL_MIN_AMT = 30, 1e12, 5e8
CAP_PCT = 3.0            # 종목당 총 비중 상한(갈래 합산)
EOK = 1e8
FDR_SYMS = {"spx": ["US500"], "gold_usd": ["GC=F"], "usdkrw": ["USD/KRW"], "vix": ["VIX"],
            "069500": ["069500"], "360750": ["360750"], "411060": ["411060"], "153130": ["153130"]}
US_KEYS = ("spx", "gold_usd", "vix")   # 미국 자료는 전날(미국) 종가만 쓴다 — 한국 날짜 d 에는 d 보다 앞선 마지막 값

# KRX 휴장일(최선 추정). 지난 날짜는 지수 이력이 정본이고, 이 목록은 '다음 거래일·월말' 추정에만 쓴다
KRX_HOLIDAYS = {
    "2025-01-01", "2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30", "2025-03-03", "2025-05-01", "2025-05-05", "2025-05-06",
    "2025-06-03", "2025-06-06", "2025-08-15", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08", "2025-10-09", "2025-12-25", "2025-12-31",
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02", "2026-05-01", "2026-05-05", "2026-05-25", "2026-06-03",
    "2026-08-17", "2026-09-24", "2026-09-25", "2026-10-05", "2026-10-09", "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-02-10", "2027-03-01", "2027-05-05", "2027-05-13", "2027-06-06", "2027-08-16",
    "2027-09-14", "2027-09-15", "2027-09-16", "2027-10-04", "2027-10-11", "2027-12-27", "2027-12-31",
}
_running = threading.Lock()


# ── 달력 ──────────────────────────────────────────────────────────────────

def _cal_next(d: str) -> str:
    x = Date.fromisoformat(d) + timedelta(days=1)
    while x.weekday() >= 5 or x.isoformat() in KRX_HOLIDAYS:
        x += timedelta(days=1)
    return x.isoformat()


class Calendar:
    """거래일 목록(지수 이력의 날짜). 목록 밖의 미래는 요일·휴장일로 추정한다."""

    def __init__(self, tdays: list[str]):
        self.tdays = list(tdays)
        self.pos = {d: i for i, d in enumerate(self.tdays)}

    def next(self, d: str) -> str:
        i = self.pos.get(d)
        if i is not None and i + 1 < len(self.tdays):
            return self.tdays[i + 1]
        return _cal_next(d)

    def shift(self, d: str, n: int) -> str:
        for _ in range(n):
            d = self.next(d)
        return d

    def is_month_end(self, d: str) -> bool:
        return self.next(d)[:7] != d[:7]

    def is_week_end(self, d: str) -> bool:
        return Date.fromisoformat(self.next(d)).isocalendar()[1] != Date.fromisoformat(d).isocalendar()[1]

    def last_month_end(self, d: str, skip_self: bool = False) -> str | None:
        i = self.pos[d] - (1 if skip_self else 0)
        while i >= 0:
            if self.is_month_end(self.tdays[i]):
                return self.tdays[i]
            i -= 1
        return None

    def last_week_end(self, d: str) -> str:
        i = self.pos[d]
        while i >= 0:
            if self.is_week_end(self.tdays[i]):
                return self.tdays[i]
            i -= 1
        return self.tdays[0]

    def next_month_end(self, d: str) -> str:
        """d 다음(포함 안 함)의 첫 월말 거래일 — 다음 코어 점검일."""
        x = d
        while True:
            x = self.next(x)
            if self.is_month_end(x):
                return x


def _at(series: list[tuple[str, float]], d: str, strict: bool = False) -> float | None:
    """d 이전(포함, strict 면 미포함) 마지막 값."""
    best = None
    for sd, v in series:
        if sd > d or (strict and sd == d):
            break
        best = v
    return best


def _f(x, nd=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


# ── 자료 ──────────────────────────────────────────────────────────────────

def ensure_index(con) -> None:
    """코스피·코스닥 일별 종가(네이버). 13개월 이상 있어야 10개월선·12개월 모멘텀을 만든다."""
    for code, name in (("KS11", "KOSPI"), ("KQ11", "KOSDAQ")):
        n = con.execute("SELECT COUNT(*) FROM px_hist WHERE code=?", (code,)).fetchone()[0]
        try:
            rows = edges.index_history(name, pages=1 if n >= 330 else 8)
        except Exception as ex:  # noqa: BLE001
            log.warning("지수 이력 실패 %s: %s", code, ex)
            continue
        con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", [(code, d, c) for d, c in rows])


def ensure_series(con, days: int = 800) -> dict[str, str]:
    """S&P·금·환율·VIX·지수 ETF 종가(FinanceDataReader) 를 series 표에 이어 붙인다. 실패하면 있는 것까지만 쓴다."""
    import FinanceDataReader as fdr
    status = {}
    for key, syms in FDR_SYMS.items():
        last = con.execute("SELECT MAX(date) FROM series WHERE key=?", (key,)).fetchone()[0]
        start = (Date.fromisoformat(last) - timedelta(days=10)).isoformat() if last else \
            (datetime.now(KST).date() - timedelta(days=days)).isoformat()
        for s in syms:
            try:
                df = fdr.DataReader(s, start)
                rows = [(key, d.strftime("%Y-%m-%d"), float(r.Close)) for d, r in df.iterrows()
                        if r.Close is not None and r.Close == r.Close and r.Close > 0]
                if not rows:
                    raise ValueError("empty")
                con.executemany("INSERT OR REPLACE INTO series(key,date,value) VALUES(?,?,?)", rows)
                status[key] = f"{s} ~{rows[-1][1]}"
                break
            except Exception as ex:  # noqa: BLE001
                status[key] = f"{s} 실패: {ex}" + (f" (저장분 ~{last})" if last else "")
                log.warning("series %s %s: %s", key, s, ex)
        time.sleep(0.2)
    return status


def series(con, key: str) -> list[tuple[str, float]]:
    return [(r[0], r[1]) for r in con.execute("SELECT date, value FROM series WHERE key=? AND value>0 ORDER BY date", (key,))]


def latest_snapshot(con) -> dict[str, dict]:
    """종목별 최근 스냅샷(이름·시장·종가·시총) + 20일 평균 거래대금 + 최근 60행 거래대금 0 일수."""
    out = {}
    for r in con.execute("""SELECT code, name, market, close, marcap FROM stock_daily
                            WHERE marcap>0 GROUP BY code HAVING date=MAX(date)"""):
        out[r["code"]] = {"name": r["name"], "market": r["market"], "close": r["close"], "marcap": r["marcap"], "amt20": 0.0, "halt": 0}
    for r in con.execute("""SELECT code, AVG(amount) a, SUM(CASE WHEN amount IS NULL OR amount<=0 THEN 1 ELSE 0 END) z FROM
                            (SELECT code, amount, ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) n FROM stock_daily)
                            WHERE n<=20 GROUP BY code"""):
        if r["code"] in out:
            out[r["code"]]["amt20"] = r["a"] or 0.0
    for r in con.execute("""SELECT code, SUM(CASE WHEN (amount IS NULL OR amount<=0) AND close>0 THEN 1 ELSE 0 END) z FROM
                            (SELECT code, amount, close, ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) n FROM stock_daily)
                            WHERE n<=60 GROUP BY code"""):
        if r["code"] in out:
            out[r["code"]]["halt"] = r["z"] or 0
    return out


def ensure_history(con, codes: list[str], asof: str, days: int = 470, workers: int = 4) -> dict:
    """월말 모멘텀·저변동 계산용 전 종목 이력(지난달 월말의 252거래일 전까지 닿도록 약 15개월). 없는 종목은 전부, 오래된 종목은 증분으로 받는다(월 1회).
    px_hist 는 수정주가(FinanceDataReader)이고 그 뒤 날짜는 stock_daily 스냅샷이 잇는다 — 단위가 어긋난 종목은 다시 받는다."""
    import FinanceDataReader as fdr
    start = (Date.fromisoformat(asof) - timedelta(days=days)).isoformat()
    last = {r[0]: r[1] for r in con.execute("SELECT code, MAX(date) FROM px_hist GROUP BY code")}
    jobs = []
    for c in dict.fromkeys(codes):
        lc = last.get(c)
        if lc is None:
            jobs.append((c, start, True))
        elif edges.has_break(edges.closes(con, c)[-250:]):
            jobs.append((c, start, True))
        elif lc < (Date.fromisoformat(asof) - timedelta(days=7)).isoformat():
            jobs.append((c, (Date.fromisoformat(lc) + timedelta(days=1)).isoformat(), False))
    stat = {"full": sum(j[2] for j in jobs), "incremental": sum(not j[2] for j in jobs), "failed": 0}
    if not jobs:
        return stat

    def work(job):
        c, s, full = job
        time.sleep(0.15)  # 비공식 경로에 부담을 주지 않는다
        try:
            df = fdr.DataReader(c, s)
            return c, full, [(c, d.strftime("%Y-%m-%d"), float(r.Close)) for d, r in df.iterrows() if r.Close and r.Close > 0]
        except Exception as ex:  # noqa: BLE001
            return c, full, str(ex)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (c, full, rows) in enumerate(ex.map(work, jobs), 1):
            if isinstance(rows, str):
                stat["failed"] += 1
                continue
            if full:
                con.execute("DELETE FROM px_hist WHERE code=?", (c,))
            con.executemany("INSERT OR REPLACE INTO px_hist(code,date,close) VALUES(?,?,?)", rows)
            if i % 200 == 0:
                con.commit()
                log.info("종목 이력 %d/%d (%.0fs)", i, len(jobs), time.time() - t0)
    con.commit()
    log.info("종목 이력 보충 끝: %s (%.0fs)", stat, time.time() - t0)
    return stat


NAVER_INTEGRATION = "https://m.stock.naver.com/api/stock/{code}/integration"


def _num(v) -> float | None:
    try:
        s = str(v).replace(",", "").replace("배", "").replace("원", "").strip()
        return float(s) if s and s not in ("N/A", "-") else None
    except ValueError:
        return None


def fetch_fund(code: str) -> dict | None:
    import requests
    r = requests.get(NAVER_INTEGRATION.format(code=code), timeout=15,
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"})
    r.raise_for_status()
    info = {x.get("code"): x.get("value") for x in (r.json().get("totalInfos") or [])}
    return {"pbr": _num(info.get("pbr")), "bps": _num(info.get("bps")), "eps": _num(info.get("eps")), "per": _num(info.get("per"))}


def ensure_fund(con, codes: list[str], asof: str, max_age_days: int = 35, workers: int = 4) -> dict:
    """PBR·BPS·EPS(네이버 종목 페이지). 저PBR 목록과 'PBR 하위 절반' 판정에 쓴다. 35일 넘은 것만 다시 받는다."""
    have = {r[0]: r[1] for r in con.execute("SELECT code, asof FROM fund")}
    limit = (Date.fromisoformat(asof) - timedelta(days=max_age_days)).isoformat()
    todo = [c for c in dict.fromkeys(codes) if have.get(c, "") < limit]
    stat = {"fetched": 0, "failed": 0, "kept": len(codes) - len(todo)}
    if not todo:
        return stat

    def work(c):
        time.sleep(0.25)
        try:
            return c, fetch_fund(c)
        except Exception as ex:  # noqa: BLE001
            return c, str(ex)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (c, res) in enumerate(ex.map(work, todo), 1):
            if isinstance(res, str):
                stat["failed"] += 1
                continue
            con.execute("INSERT OR REPLACE INTO fund(code,asof,pbr,bps,eps,per) VALUES(?,?,?,?,?,?)",
                        (c, asof, res["pbr"], res["bps"], res["eps"], res["per"]))
            stat["fetched"] += 1
            if i % 200 == 0:
                con.commit()
    con.commit()
    log.info("재무 보충 끝: %s", stat)
    return stat


# ── 공통 지표 ───────────────────────────────────────────────────────────────

def kq120(kq: list[tuple[str, float]], d: str) -> dict:
    px = [c for sd, c in kq if sd <= d]
    if len(px) < 120:
        return {"date": d, "kosdaq": _f(px[-1]) if px else None, "ma120": None, "on": None}
    ma = sum(px[-120:]) / 120
    return {"date": d, "kosdaq": _f(px[-1]), "ma120": _f(ma), "on": bool(px[-1] > ma)}


def vix_ok(vix: list[tuple[str, float]], d: str) -> dict:
    v = _at(vix, d, strict=True)  # 전날(미국) 값
    return {"date": d, "vix_t-1": _f(v), "on": bool(v < 25) if v is not None else None}


def usd_ok(usd: list[tuple[str, float]], d: str, cal: Calendar) -> dict:
    v = _at(usd, d)
    i = cal.pos.get(d)
    if v is None or i is None or i < 60:
        return {"date": d, "usdkrw": _f(v), "chg60": None, "on": None}
    v0 = _at(usd, cal.tdays[i - 60])
    if not v0:
        return {"date": d, "usdkrw": _f(v), "chg60": None, "on": None}
    chg = v / v0 - 1
    return {"date": d, "usdkrw": _f(v), "chg60": _f(chg * 100), "on": bool(chg <= 0.03)}


def risk_on(ctx: "Ctx", d: str) -> dict:
    a, b, c = kq120(ctx.kq, d), vix_ok(ctx.vix, d), usd_ok(ctx.usd, d, ctx.cal)
    on = None if None in (a["on"], b["on"], c["on"]) else bool(a["on"] and b["on"] and c["on"])
    return {"date": d, "kq120": a, "vix": b, "usdkrw": c, "on": on}


def _closes_upto(con, code: str, d: str, n: int) -> list[float]:
    return [c for sd, c in edges.closes(con, code, 400) if sd <= d][-n:]


def _std(xs: list[float]) -> float | None:
    return statistics.pstdev(xs) if len(xs) >= 2 else None


class Ctx:
    """한 번의 계산에 쓰는 자료 묶음."""

    def __init__(self, con, asof: str | None = None):
        self.con = con
        ks = edges.closes(con, "KS11", 5000)
        if len(ks) < 60:
            raise RuntimeError("코스피 지수 이력이 부족합니다(네이버 지수 시세 수집 실패?)")
        self.ks = ks
        self.kq = edges.closes(con, "KQ11", 5000)
        self.cal = Calendar([d for d, _ in ks])
        self.asof = asof or self.cal.tdays[-1]
        if self.asof not in self.cal.pos:
            raise RuntimeError(f"{self.asof} 는 거래일 목록에 없습니다")
        self.spx, self.gold, self.usd, self.vix = (series(con, k) for k in ("spx", "gold_usd", "usdkrw", "vix"))
        self.etf_px = {c: series(con, c) for c in (*CORE.values(), CASH)}
        self.snap = latest_snapshot(con)
        self.fund = {r["code"]: dict(r) for r in con.execute("SELECT * FROM fund")}
        self.notes: list[str] = []

    def name(self, code: str) -> str:
        return NAMES.get(code) or (self.snap.get(code) or {}).get("name") or code

    def price(self, code: str) -> float | None:
        if code in self.etf_px and self.etf_px[code]:
            return _at(self.etf_px[code], self.asof)
        s = self.snap.get(code)
        if s and s["close"]:
            return float(s["close"])
        px = _closes_upto(self.con, code, self.asof, 1)
        return px[-1] if px else None


# ── 1. 코어 70% — Faber 10개월선 ─────────────────────────────────────────────

def faber(ctx: Ctx, state: dict) -> dict:
    """매월 마지막 거래일 종가: 코스피 · S&P500×환율 · 금×환율 각각을 최근 10개월 월말 값 평균과 비교. 위면 23.3% 보유, 아니면 단기채.
    월중에는 지난 월말 판정을 그대로 둔다. 체결은 판정 다음 거래일 종가(월말 저녁에 계산 → 다음 달 첫 거래일)."""
    cal = ctx.cal
    st = state.setdefault("core", {"legs": {}, "signal_date": None})
    mdate = cal.last_month_end(ctx.asof)
    mends = [d for d in cal.tdays if cal.is_month_end(d) and d <= mdate][-FABER_MONTHS:]
    legs, missing = {}, []
    for leg, code in CORE.items():
        vals = []
        for m in mends:
            u = _at(ctx.usd, m)
            if leg == "KS":
                v = _at(ctx.ks, m)
            elif leg == "SPX":
                s = _at(ctx.spx, m, strict=True); v = s * u if (s and u) else None
            else:
                g = _at(ctx.gold, m, strict=True); v = g * u if (g and u) else None
            vals.append(v)
        ok = len(vals) == FABER_MONTHS and all(v is not None for v in vals)
        if ok:
            ma = sum(vals) / len(vals)
            on = vals[-1] > ma
            legs[leg] = {"code": code, "name": NAMES[code], "on": bool(on), "value": _f(vals[-1]), "ma10": _f(ma),
                         "gap_pct": _f((vals[-1] / ma - 1) * 100), "months": len(vals)}
        else:
            prev = (st["legs"].get(leg) or {}).get("on")
            missing.append(leg)
            legs[leg] = {"code": code, "name": NAMES[code], "on": prev, "value": _f(vals[-1]) if vals else None, "ma10": None,
                         "gap_pct": None, "months": sum(v is not None for v in vals),
                         "note": "자료 부족 — " + ("지난 판정 유지" if prev is not None else "판정 불가(현금)")}
    if missing:
        ctx.notes.append(f"코어 {', '.join(missing)} 자료가 모자라 10개월 평균을 못 만들었다")
    st["legs"] = {k: {"on": v["on"]} for k, v in legs.items()}
    st["signal_date"] = mdate
    per = W["core"] / 3
    targets = {}
    cash = 0.0
    for leg, v in legs.items():
        if v["on"]:
            targets[v["code"]] = round(per, 2)
        else:
            cash += per
    if cash:
        targets[CASH] = round(cash, 2)
    return {"weight": W["core"], "signal_date": mdate, "exec_date": cal.next(mdate), "is_month_end_today": cal.is_month_end(ctx.asof),
            "next_check": cal.next_month_end(ctx.asof) if not cal.is_month_end(ctx.asof) else ctx.asof,
            "legs": legs, "targets": targets, "month_ends": mends,
            "note": "각 다리 23.3%. 꺼진 다리 몫은 KODEX 단기채권(153130)·CMA — 다른 다리로 옮기지 않는다. 월중에는 코어를 건드리지 않는다."}


# ── 2. 저변동 모멘텀 10% ─────────────────────────────────────────────────────

def momentum_list(ctx: Ctx, d: str) -> dict:
    """월말 d: 전 종목 12-1개월 모멘텀 상위 200 ∩ 시총 상위 200(20일 거래대금 10억↑) → 조작·거래정지 패턴 제외 → 60일 변동성 낮은 40."""
    con, cal = ctx.con, ctx.cal
    i = cal.pos[d]
    if i < 252:
        return {"date": d, "list": [], "error": "지수 이력 252일 미만"}
    d21, d252 = cal.tdays[i - 21], cal.tdays[i - 252]
    # 두 날짜 종가(전 종목): px_hist 우선, 없으면 그 날 스냅샷
    px = {}
    for dd in (d21, d252, d):
        px[dd] = {r[0]: r[1] for r in con.execute("SELECT code, close FROM stock_daily WHERE date=? AND close>0", (dd,))}
        px[dd].update({r[0]: r[1] for r in con.execute("SELECT code, close FROM px_hist WHERE date=? AND close>0", (dd,))})
    codes = [c for c in ctx.snap if c in px[d21] and c in px[d252]]
    mom = {c: px[d21][c] / px[d252][c] - 1 for c in codes}
    pool = set(sorted(mom, key=lambda c: -mom[c])[:MOM_POOL])
    by_mcap = sorted(ctx.snap, key=lambda c: -ctx.snap[c]["marcap"])
    top = {c for c in by_mcap[:MOM_TOP_MCAP] if ctx.snap[c]["amt20"] >= MOM_MIN_AMT}
    cand = [c for c in codes if c in pool and c in top]
    # 조작 패턴('꾸준상승'): 12개월 +100% & 수익/변동성(120일) ≥4 & 회전율(20일 거래대금/시총) 하위 70% — 시총 300억↑ 안에서
    turn = {c: ctx.snap[c]["amt20"] / ctx.snap[c]["marcap"] for c in ctx.snap if ctx.snap[c]["marcap"] >= 300 * EOK and ctx.snap[c]["amt20"] > 0}
    q70 = sorted(turn.values())[int(len(turn) * 0.7)] if turn else float("inf")
    rows, excluded = [], []
    for c in cand:
        cl = _closes_upto(con, c, d, 121)
        if len(cl) < 61:
            continue
        r12 = px[d][c] / px[d252][c] - 1 if c in px[d] else mom[c]
        rets120 = [b / a - 1 for a, b in zip(cl, cl[1:])]
        vol120 = (_std(rets120) or 0) * math.sqrt(252)
        manip = r12 > 1.0 and vol120 > 0 and r12 / vol120 >= 4.0 and turn.get(c, float("inf")) <= q70
        halt = ctx.snap[c]["halt"] >= 5
        if manip or halt:
            excluded.append({"code": c, "name": ctx.name(c), "why": "조작 패턴" if manip else "거래정지 의심"})
            continue
        rets60 = rets120[-60:]
        v60 = _std(rets60)
        if v60 is None:
            continue
        rows.append({"code": c, "name": ctx.name(c), "mom12_1": _f(mom[c] * 100, 1), "vol60_d": _f(v60 * 100, 3), "mcap_eok": _f(ctx.snap[c]["marcap"] / EOK, 0), "_v": v60})
    rows.sort(key=lambda r: r.pop("_v"))   # 표시용 반올림 값이 아니라 원래 변동성으로 정렬한다
    rows = rows[:MOM_N]
    out = {"date": d, "list": [r["code"] for r in rows], "rows": rows, "n_pool_in_universe": len(cand), "n_universe": len(top),
           "excluded": excluded, "d21": d21, "d252": d252, "n_priced": len(codes)}
    if len(rows) < 5:
        out["error"] = f"후보 {len(rows)}개(<5) → 이 달은 빈 목록(현금)"; out["list"] = []
    return out


def momentum(ctx: Ctx, state: dict) -> dict:
    cal = ctx.cal
    mdate = cal.last_month_end(ctx.asof)
    prev_m = cal.last_month_end(mdate, skip_self=True)
    wdate = cal.last_week_end(ctx.asof)
    st = state.setdefault("mom", {"lists": {}, "rows": {}, "gate_on": None, "gate_date": None})
    diag = {}
    for d in (prev_m, mdate):
        if d and not st["lists"].get(d):
            r = momentum_list(ctx, d)
            diag[d] = {k: v for k, v in r.items() if k != "rows"}
            if r.get("error"):   # 자료가 모자라 못 만든 달은 저장하지 않는다 — 다음 실행(이력 보충 뒤)에 다시 시도
                ctx.notes.append(f"모멘텀 {d}: {r['error']}")
                continue
            st["lists"][d] = r["list"]; st["rows"][d] = r.get("rows", [])
    cur, old = st["lists"].get(mdate, []), st["lists"].get(prev_m, []) if prev_m else []
    w = {}
    for lst in (cur, old):
        for c in lst:
            w[c] = w.get(c, 0.0) + (0.5 / len(lst) if lst else 0.0)
    g = kq120(ctx.kq, wdate)
    transition = st.get("gate_on") is not None and g["on"] is not None and g["on"] != st["gate_on"]
    on = bool(g["on"])
    targets = {c: round(W["mom"] * v, 3) for c, v in w.items()} if on else {}
    cash = W["mom"] - (sum(w.values()) * W["mom"] if on else 0.0)
    targets[CASH] = round(cash, 3)
    rows = [{"code": c, "name": ctx.name(c), "tranche": "이번달+지난달" if c in cur and c in old else "이번달" if c in cur else "지난달",
             "target_pct": targets.get(c, 0.0)} for c in sorted(w, key=lambda c: -w[c])]
    st.update({"gate_on": on, "gate_date": wdate})
    for k in sorted(st["lists"])[:-3]:
        st["lists"].pop(k, None); st["rows"].pop(k, None)
    return {"weight": W["mom"], "month_end": mdate, "prev_month_end": prev_m, "gate": g, "gate_on": on, "gate_transition": transition,
            "holdings": rows, "this_month": [{"code": c, "name": ctx.name(c)} for c in cur], "last_month": [{"code": c, "name": ctx.name(c)} for c in old],
            "targets": targets, "diag": diag, "rows_this_month": st["rows"].get(mdate, []),
            "note": "2트랜치(이번 달 목록 절반 + 지난 달 목록 절반). 코스닥이 120일선 아래면(주 마지막 거래일 점검) 전량 단기채, 전환될 때만 체결."}


# ── 3. ETF추가 주도주 8% ─────────────────────────────────────────────────────

def etf_adds(con, d: str) -> dict[str, dict]:
    """기준일 d 에 ETF 가 새로 담았거나 1CU당 주식수를 10% 이상 늘린 종목 → {code: {n_add, n_new, etfs, theme_buy}}."""
    out = {}
    for r in con.execute("""SELECT c.stock_code code, c.kind, c.pct, e.name etf_name, e.theme FROM change c JOIN etf e ON e.code=c.etf_code
                            WHERE c.date=? AND (c.kind='NEW' OR (c.kind='UP' AND c.pct>=0.10))""", (d,)):
        x = out.setdefault(r["code"], {"n_add": 0, "n_new": 0, "etfs": [], "theme_buy": False})
        x["n_add"] += 1
        x["n_new"] += r["kind"] == "NEW"
        if len(x["etfs"]) < 3:
            x["etfs"].append(r["etf_name"])
        x["theme_buy"] = x["theme_buy"] or (r["theme"] not in edges.NOT_THEME)
    return out


def etf_leader(ctx: Ctx, state: dict) -> dict:
    con, cal = ctx.con, ctx.cal
    st = state.setdefault("etf", {"open": [], "last_obs": None, "closed": []})
    obs = db.get_meta(con, "computed_date")
    out = {"weight": W["etf"], "slots": ETF_SLOTS, "position_pct": round(W["etf"] / ETF_SLOTS, 2), "obs": obs, "candidates": [], "near_miss": [],
           "opened_now": [], "exits_now": [], "gate": None}
    if obs and obs in cal.pos:
        adds = etf_adds(con, obs)
        # PBR 하위 절반: 시총 300억↑ 종목의 PBR 중앙값
        pbrs = sorted(v["pbr"] for c, v in ctx.fund.items() if v.get("pbr") and v["pbr"] > 0 and (ctx.snap.get(c) or {}).get("marcap", 0) >= 300 * EOK)
        median_pbr = pbrs[len(pbrs) // 2] if len(pbrs) >= 200 else None
        idx = ctx.ks
        cands = []
        for c, a in adds.items():
            s = ctx.snap.get(c)
            if not s or s["marcap"] < 300 * EOK or s["amt20"] <= 0:
                continue
            px = [(sd, v) for sd, v in edges.closes(con, c, 400) if sd <= obs]
            rel60, hi52 = edges.features(px, idx if s["market"] == "KOSPI" else ctx.kq)
            if rel60 is None or hi52 is None:
                continue
            pbr = (ctx.fund.get(c) or {}).get("pbr")
            cheap = bool(median_pbr and pbr and 0 < pbr <= median_pbr)
            ok = rel60 > edges.LEAD and hi52 >= edges.NEAR_HIGH and not cheap
            cap_eok = s["marcap"] / EOK
            cands.append({"code": c, "name": ctx.name(c), "n_add": a["n_add"], "n_new": a["n_new"], "etfs": ", ".join(a["etfs"]),
                          "rel60_pct": _f(rel60 * 100, 1), "hi52": _f(hi52, 3), "pbr": _f(pbr), "cheap": cheap, "mcap_eok": _f(cap_eok, 0),
                          "theme_small": bool(a["theme_buy"] and cap_eok < edges.SMALL_CAP_EOK), "close_obs": _f(px[-1][1], 0) if px else None, "pass": bool(ok)})
        passed = sorted([c for c in cands if c["pass"]], key=lambda x: (-x["theme_small"], -x["rel60_pct"]))
        out.update({"n_added": len(adds), "n_with_history": len(cands), "candidates": passed, "median_pbr": _f(median_pbr),
                    "near_miss": sorted([c for c in cands if not c["pass"] and c["rel60_pct"] > 10 and c["hi52"] > 0.85], key=lambda x: -x["rel60_pct"])[:10]})
        g = risk_on(ctx, obs); out["gate"] = g
        if not median_pbr:
            ctx.notes.append("PBR 자료가 아직 없어 ETF주도주의 'PBR 하위 절반 제외'를 적용하지 못했다")
        if st.get("last_obs") != obs:
            opened = []
            if g["on"]:
                have = {p["code"] for p in st["open"]}
                free = ETF_SLOTS - len(st["open"])
                exec_date = cal.next(ctx.asof)
                for c in passed:
                    if free <= 0:
                        break
                    if c["code"] in have:
                        continue
                    pos = {"code": c["code"], "name": c["name"], "obs": obs, "entry_date": exec_date, "entry_price": None,
                           "stop": None, "take": None, "expiry": cal.shift(exec_date, ETF_HOLD), "weight_pct": round(W["etf"] / ETF_SLOTS, 2),
                           "status": "pending", "why": ("테마·중소형 주도주" if c["theme_small"] else "주도주") + f" · 60일 시장대비 {c['rel60_pct']:+}% · 52주고가 {round(c['hi52'] * 100)}%"}
                    st["open"].append(pos); opened.append(pos); free -= 1
            st["last_obs"] = obs
            out["opened_now"] = opened
    else:
        out["warn"] = "ETF 구성종목 기준일이 아직 없다 — 첫 수집이 끝나면 후보가 나온다"
        ctx.notes.append("ETF주도주: " + out["warn"])
    # 보유분 점검(매일): 체결가 확정 → 손절·익절·만기
    exits = []
    for p in list(st["open"]):
        px = [(sd, v) for sd, v in edges.closes(con, p["code"], 400) if sd <= ctx.asof]
        if not px:
            continue
        if p["status"] == "pending":
            if p["entry_date"] <= ctx.asof:
                ep = _at(px, p["entry_date"])
                if ep:
                    p.update({"entry_price": _f(ep, 0), "stop": _f(ep * (1 - ETF_STOP), 0), "take": _f(ep * (1 + ETF_TAKE), 0), "status": "open"})
            else:
                continue
        if not p.get("entry_price"):
            continue
        cur = px[-1][1]; ret = cur / p["entry_price"] - 1
        p["last"] = _f(cur, 0); p["ret_pct"] = _f(ret * 100, 1)
        reason = None
        if ret <= -ETF_STOP:
            reason = f"손절 {ret * 100:+.1f}%"
        elif ret >= ETF_TAKE:
            reason = f"익절 {ret * 100:+.1f}%"
        elif ctx.asof >= p["expiry"]:
            reason = f"만기 {ETF_HOLD}일 {ret * 100:+.1f}%"
        if reason:
            p["exit_reason"] = reason; p["exit_date"] = cal.next(ctx.asof); exits.append(p)
            st["open"].remove(p); st["closed"].append(p)
    st["closed"] = st["closed"][-60:]
    out["exits_now"] = exits
    out["open"] = st["open"]
    out["closed_recent"] = st["closed"][-10:][::-1]
    out["targets"] = {p["code"]: p["weight_pct"] for p in st["open"]}
    out["targets"][CASH] = round(W["etf"] - sum(p["weight_pct"] for p in st["open"]), 2)
    out["note"] = "구성종목 기준일에 ETF가 늘린 종목 중 60일 시장대비 +20%↑ · 52주고가 90%↑ · PBR 하위 절반 제외. 위험선호(코스닥 120일선 위 · VIX<25 · 환율 60일 ≤+3%)일 때만 신규. 60일 보유, 손절 −15%, 익절 +30%, 종가 기준. 슬롯 20(각 0.4%)."
    return out


# ── 4. 코스피 3일 급락 반전 6% ────────────────────────────────────────────────

def crash(ctx: Ctx, state: dict) -> dict:
    cal = ctx.cal
    st = state.setdefault("crash", {"hold_until": None, "signal_date": None, "entry_date": None})
    i = cal.pos[ctx.asof]
    ks = [c for _, c in ctx.ks]
    r3 = ks[i] / ks[i - 3] - 1
    hist = {cal.tdays[j]: _f((ks[j] / ks[j - 3] - 1) * 100) for j in range(max(3, i - 4), i + 1)}
    sig = r3 < CRASH_THR
    holding = st["hold_until"] is not None and st["hold_until"] > ctx.asof
    nxt = cal.next(ctx.asof)
    action = "신호 없음 · 보유 없음"
    if sig:
        st["signal_date"] = ctx.asof
        st["hold_until"] = cal.shift(ctx.asof, CRASH_HOLD + 1)   # s+1 종가 매수, s+4 종가 매도(3거래일 보유)
        if not holding:
            st["entry_date"] = nxt
            action = f"매수 KODEX 200 — {nxt} 종가 (3거래일 보유, 매도 {st['hold_until']} 종가)"
        else:
            action = f"연장 — 매도일 {st['hold_until']} 종가로"
        holding = True
    elif holding and nxt >= st["hold_until"]:
        action = f"매도 KODEX 200 — {st['hold_until']} 종가"
    elif holding:
        action = f"보유 유지 (매도 예정 {st['hold_until']} 종가)"
    target_on = holding and nxt < st["hold_until"]
    if not holding:
        st["hold_until"] = None; st["entry_date"] = None
    targets = {CORE["KS"]: round(W["crash"], 2)} if target_on else {CASH: round(W["crash"], 2)}
    return {"weight": W["crash"], "signal_date": ctx.asof, "ret3d_pct": _f(r3 * 100), "threshold_pct": CRASH_THR * 100, "signal": bool(sig),
            "ret3d_history_pct": hist, "action": action, "state": dict(st), "targets": targets, "kospi": _f(ks[i]),
            "note": "코스피 3일 누적 −2.5% 이하 마감 → 다음 날 종가 KODEX 200, 3거래일 보유 후 종가 매도. 필터 없음. 이 갈래 수익의 절반이 2026년 닷새에 몰려 있었다 — 기대치는 낮게."}


# ── 5. 대형 저PBR 6% ─────────────────────────────────────────────────────────

def lowpbr_list(ctx: Ctx, d: str) -> dict:
    if not ctx.fund:
        return {"date": d, "list": [], "rows": [], "error": "PBR 자료 없음(재무 보충 전)"}
    rows = []
    for c, s in ctx.snap.items():
        f = ctx.fund.get(c)
        if not f or s["marcap"] < VAL_MIN_MCAP or s["amt20"] < VAL_MIN_AMT:
            continue
        if not f.get("pbr") or f["pbr"] <= 0 or f.get("eps") is None or f["eps"] <= 0:   # ROE>0 ⇔ EPS>0 (BPS>0 일 때)
            continue
        cl = _closes_upto(ctx.con, c, d, 26)
        if any(abs(b / a - 1) > 0.40 for a, b in zip(cl, cl[1:])):
            continue   # 미보정 이벤트(액면분할 등)
        rows.append({"code": c, "name": ctx.name(c), "pbr": _f(f["pbr"]), "eps": _f(f["eps"], 0), "mcap_eok": _f(s["marcap"] / EOK, 0), "fund_asof": f["asof"]})
    rows.sort(key=lambda r: r["pbr"])
    rows = rows[:VAL_N]
    return {"date": d, "list": [r["code"] for r in rows], "rows": rows, "n_universe": len(rows)}


def lowpbr(ctx: Ctx, state: dict) -> dict:
    cal = ctx.cal
    mdate = cal.last_month_end(ctx.asof)
    st = state.setdefault("val", {"lists": {}, "rows": {}, "gates": {}})
    if mdate not in st["lists"] or (not st["lists"][mdate] and ctx.fund):
        r = lowpbr_list(ctx, mdate)
        st["lists"][mdate] = r["list"]; st["rows"][mdate] = r["rows"]; st["gates"][mdate] = vix_ok(ctx.vix, mdate)
        if r.get("error"):
            ctx.notes.append("저PBR: " + r["error"])
    lst, rows, g = st["lists"][mdate], st["rows"].get(mdate, []), st["gates"].get(mdate) or vix_ok(ctx.vix, mdate)
    on = bool(g["on"])
    per = W["val"] / VAL_N
    targets = {c: round(per, 3) for c in lst} if on else {}
    targets[CASH] = round(W["val"] - (per * len(lst) if on else 0.0), 3)
    for k in sorted(st["lists"])[:-3]:
        st["lists"].pop(k, None); st["rows"].pop(k, None); st["gates"].pop(k, None)
    return {"weight": W["val"], "month_end": mdate, "gate": g, "gate_on": on, "n": len(lst), "rows": rows, "targets": targets,
            "note": "월말: 시총 1조↑ · 흑자(EPS>0) 중 PBR 최저 30 동일가중. 월말 전날 VIX<25 이면 보유, 아니면 그 달은 단기채(주중 점검 금지)."}


# ── 합치기 ─────────────────────────────────────────────────────────────────

def combine(ctx: Ctx, S: dict) -> dict:
    per: dict[str, dict[str, float]] = {}
    for name, s in S.items():
        for c, v in (s.get("targets") or {}).items():
            if v:
                per.setdefault(c, {})[name] = float(v)
    dups = []
    for c, d in per.items():
        if c in NAMES:
            continue   # 지수 ETF·단기채는 여러 갈래가 같이 들 수 있다
        stock_sleeves = [k for k in d if k in ("mom", "etf", "val")]
        if len(stock_sleeves) > 1:   # 같은 종목이 두 종목 갈래에 겹치면 한 갈래만(큰 쪽), 나머지는 그 갈래의 현금으로
            keep = max(stock_sleeves, key=lambda k: d[k])
            for k in stock_sleeves:
                if k != keep:
                    dups.append(f"{ctx.name(c)}: {k} {d[k]:.2f}% → 단기채({keep} 유지)")
                    per.setdefault(CASH, {})[k] = per.get(CASH, {}).get(k, 0.0) + d[k]
                    d[k] = 0.0
    rows = []
    for c, d in per.items():
        tot = sum(d.values())
        if tot <= 0:
            continue
        rows.append({"code": c, "name": ctx.name(c), "pct": round(tot, 3), "by": {k: round(v, 3) for k, v in d.items() if v > 0},
                     "price": ctx.price(c), "cap_ok": tot <= CAP_PCT + 1e-9 or c in NAMES})
    rows.sort(key=lambda r: -r["pct"])
    ks_core = (S["core"]["targets"] or {}).get(CORE["KS"], 0.0)
    ks_crash = (S["crash"]["targets"] or {}).get(CORE["KS"], 0.0)
    return {"rows": rows, "total_pct": round(sum(r["pct"] for r in rows), 2), "cash_pct": round(sum(per.get(CASH, {}).values()), 2),
            "kospi_exposure": {"core": ks_core, "crash": ks_crash, "sum": round(ks_core + ks_crash, 2)},
            "cap_violations": [r["name"] for r in rows if not r["cap_ok"]], "duplicates": dups, "n_names": len(rows)}


def diff_targets(prev: dict | None, rows: list[dict], ctx: Ctx) -> dict:
    cur = {r["code"]: r["pct"] for r in rows}
    price = {r["code"]: r["price"] for r in rows}
    if not prev:
        return {"has_prev": False, "orders": [{"code": c, "name": ctx.name(c), "side": "BUY", "from_pct": 0.0, "to_pct": v, "delta_pct": v, "price": price.get(c)} for c, v in cur.items()]}
    pt = prev.get("targets", {})
    orders = []
    for c in sorted(set(cur) | set(pt), key=lambda x: -(cur.get(x, 0) + pt.get(x, 0))):
        a, b = pt.get(c, 0.0), cur.get(c, 0.0)
        if abs(b - a) < 0.005:
            continue
        orders.append({"code": c, "name": ctx.name(c), "side": "BUY" if b > a else "SELL", "from_pct": round(a, 3), "to_pct": round(b, 3),
                       "delta_pct": round(b - a, 3), "price": price.get(c) or ctx.price(c)})
    return {"has_prev": True, "prev_date": prev.get("date"), "orders": orders}


def load_state(con) -> dict:
    raw = db.get_meta(con, "plan_state")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


# ── 실행 ─────────────────────────────────────────────────────────────────────

def refresh_data(con, asof_hint: str | None = None) -> dict:
    """매일: 지수·시리즈. 월말 목록이 새로 필요하거나 자료가 비어 있으면 전 종목 이력·재무까지."""
    ensure_index(con)
    status = {"series": ensure_series(con)}
    con.commit()
    ctx = Ctx(con)
    mdate = ctx.cal.last_month_end(ctx.asof)
    state = load_state(con)
    need_month = bool(mdate) and not (state.get("mom") or {}).get("lists", {}).get(mdate)
    codes = [c for c, s in ctx.snap.items() if s["marcap"] > 0]
    if need_month and codes:
        status["history"] = ensure_history(con, codes, ctx.asof)
    if codes and (need_month or not ctx.fund):
        big = [c for c in codes if ctx.snap[c]["marcap"] >= 300 * EOK]
        status["fund"] = ensure_fund(con, big, ctx.asof)
    return status


def compute(con, asof: str | None = None, save: bool = True) -> dict:
    """신호일 asof(기본: 마지막 거래일) 종가 기준 계획. save=False 면 상태를 저장하지 않는다."""
    t0 = time.time()
    ctx = Ctx(con, asof)
    state = load_state(con)
    S = {}
    for name, fn in (("core", faber), ("mom", momentum), ("etf", etf_leader), ("crash", crash), ("val", lowpbr)):
        try:
            S[name] = fn(ctx, state)
        except Exception as ex:  # noqa: BLE001
            log.exception("%s 갈래 계산 실패", name)
            S[name] = {"error": f"{type(ex).__name__}: {ex}", "targets": {}, "weight": W[name]}
            ctx.notes.append(f"{name} 갈래 계산 실패: {ex}")
    C = combine(ctx, S)
    prev = state.get("last")
    if prev and prev.get("date", "") > ctx.asof:
        ctx.notes.append(f"마지막 계획({prev['date']})이 신호일보다 뒤다 — 과거 날짜를 다시 계산한 것이면 주문표는 참고만")
    diff = diff_targets(prev, C["rows"], ctx)
    exec_date = ctx.cal.next(ctx.asof)
    failed = [k for k, s in S.items() if s.get("error")]
    plan = {"asof": ctx.asof, "exec_date": exec_date, "generated": datetime.now(KST).isoformat(timespec="seconds"),
            "is_month_end": ctx.cal.is_month_end(ctx.asof), "next_month_end": S["core"].get("next_check"),
            "weights": W, "sleeves": S, "combined": C, "diff": diff, "notes": ctx.notes, "failed": failed,
            "data": {"series_last": {k: (series(con, k)[-1][0] if series(con, k) else None) for k in ("spx", "gold_usd", "usdkrw", "vix")},
                     "kospi_last": ctx.ks[-1][0], "n_stocks": len(ctx.snap), "n_fund": len(ctx.fund),
                     "px_hist_codes": con.execute("SELECT COUNT(DISTINCT code) FROM px_hist").fetchone()[0]},
            "runtime_s": round(time.time() - t0, 1)}
    if save:
        if failed:
            ctx.notes.append(f"갈래 실패 {failed} → 이번 목표는 상태에 남기지 않는다")
        else:
            state["last"] = {"date": ctx.asof, "exec_date": exec_date, "targets": {r["code"]: r["pct"] for r in C["rows"]}, "generated": plan["generated"]}
        db.set_meta(con, "plan_state", json.dumps(state, ensure_ascii=False))
        db.set_meta(con, "plan", json.dumps(plan, ensure_ascii=False))
        con.execute("INSERT OR REPLACE INTO plan_log(date,exec_date,json) VALUES(?,?,?)", (ctx.asof, exec_date, json.dumps(plan, ensure_ascii=False)))
    return plan


def latest(con) -> dict | None:
    raw = db.get_meta(con, "plan")
    return json.loads(raw) if raw else None


def run() -> dict | None:
    """수집 뒤 한 번: 자료 보충 → 계획 계산 → 저장. 동시 실행 방지."""
    if not _running.acquire(blocking=False):
        return None
    try:
        with db.session() as con:
            status = refresh_data(con)
        with db.session() as con:
            plan = compute(con)
            plan["data"]["refresh"] = status
            db.set_meta(con, "plan", json.dumps(plan, ensure_ascii=False))
        log.info("투자 계획 %s → 체결 %s, 주문 %d건, %ss", plan["asof"], plan["exec_date"], len(plan["diff"]["orders"]), plan["runtime_s"])
        return plan
    except Exception:  # noqa: BLE001
        log.exception("투자 계획 계산 실패")
        return None
    finally:
        _running.release()


def running() -> bool:
    return _running.locked()
