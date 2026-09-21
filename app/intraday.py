"""장중(평일 09:00~15:35) 레이어. 구성종목은 하루 한 번만 나오므로, 장중에 실시간으로 볼 수 있는 것은 시세와 시장 수급이다.

아침에 뜬 시그널 종목과 핫 ETF의 현재가, 지수, 시장 전체의 금융투자(증권사) 순매수 흐름을 1분마다 갱신한다.
전부 키가 필요 없는 네이버 조회. 종목별 장중 수급은 키 없이 받을 곳이 없다 — 증권사 키가 생기면 여기에 붙인다.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from . import build, config, db, notifier, sources

log = logging.getLogger("intraday")
KST = ZoneInfo("Asia/Seoul")
MARKETS = ("KOSPI", "KOSDAQ")
_state = {"open": False, "at": None, "day": None, "quotes": {}, "etfs": [], "market": {}, "index": {}, "series": {}}
_alerted: set[tuple[str, str]] = set()  # (날짜, 종목) — 하루 한 번만 알린다
_lock = threading.Lock()


def market_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) <= (15, 35)


def snapshot() -> dict:
    with _lock:
        s = dict(_state)
    # 그래프에는 80점이면 충분하다 — 하루 400점을 그대로 보내지 않는다
    s["series"] = {m: pts[::max(1, len(pts) // 80)] + pts[-1:] if pts else [] for m, pts in s["series"].items()}
    return s


def watch_codes(b: dict) -> list[str]:
    codes = b["signals"]["up"] + b["signals"]["down"] + b["watch"] + b.get("picks", []) + b["rank"]
    return list(dict.fromkeys(codes))[:config.WATCH_LIMIT]


def tick() -> None:
    now = datetime.now(KST)
    day = now.strftime("%Y%m%d")
    live = market_hours(now)
    with _lock:
        have_today = _state["day"] == day and bool(_state["quotes"])
    # 장이 끝났어도(또는 서버를 다시 켰어도) 그날 값이 비어 있으면 한 번은 채운다 — 저녁에 열어도 오늘 흐름이 보이게
    if not live and (have_today or now.weekday() >= 5 or now.hour < 9):
        with _lock:
            _state["open"] = False
        return
    try:
        b = build.cached()
        rt = sources.realtime(watch_codes(b)) if b.get("asof") else {"open": False, "quotes": {}}
        hot = {e["code"] for e in b.get("hotEtfs", [])}
        etfs = [{"code": e["code"], "name": e["name"], "chg": e["chg"],
                 "gap": round((e["price"] / e["nav"] - 1) * 100, 2) if e["price"] and e["nav"] else None}
                for e in sources.etf_list() if e["code"] in hot]  # 괴리율: 돈이 몰리는 ETF는 NAV보다 비싸게 거래된다
        index = sources.index_quotes()
    except Exception as ex:
        log.warning("장중 시세 실패: %s", ex)
        return
    market, series = {}, {}
    if config.MARKET_FLOWS:  # 시세와 따로 감싼다 — 수급이 깨져도 시세는 계속 나가야 한다
        with _lock:
            old = _state["series"] if _state["day"] == day else {}
        for m in MARKETS:
            try:
                flows = sources.market_flows(m, day)
                if not flows:
                    continue
                market[m] = flows
                pts = old.get(m) or sources.market_flow_series(m, day)  # 하루치는 그날 처음 한 번만 받는다
                if not pts or pts[-1]["time"] != flows["time"]:
                    pts = pts + [{k: flows[k] for k in ("time", "fin", "inst", "foreign", "indiv")}]
                series[m] = pts
            except Exception as ex:
                log.warning("%s 시장 수급 실패: %s", m, ex)
    with _lock:
        _state.update(open=rt["open"], at=now.isoformat(timespec="seconds"), day=day, quotes=rt["quotes"], index=index,
                      etfs=sorted(etfs, key=lambda e: -(e["chg"] or 0)),
                      market=market or _state["market"], series=series or _state["series"])
    if rt["open"]:
        _alert(b, rt["quotes"])


def _alert(b: dict, quotes: dict) -> None:
    day = datetime.now(KST).strftime("%Y-%m-%d")
    for code in b["signals"]["up"] + b["watch"]:
        q, s = quotes.get(code), b["stocks"].get(code)
        if not q or not s or (day, code) in _alerted or abs(q["chg"]) < config.INTRADAY_ALERT_PCT:
            continue
        _alerted.add((day, code))
        arrow = "▲" if q["chg"] > 0 else "▼"
        notifier.send(f"**{s['name']}** {arrow} {q['chg']:+.1f}% (현재 {q['price']:,.0f}원)\n"
                      f"아침 시그널: {s['why']}" + notifier.link(f"/#stock/{code}"))


def watch_add(code: str) -> None:
    with db.session() as con:
        con.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (code, datetime.now(KST).isoformat(timespec="seconds")))


def watch_remove(code: str) -> None:
    with db.session() as con:
        con.execute("DELETE FROM watch WHERE code=?", (code,))
