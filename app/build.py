"""DB → 화면이 한 번에 받아 가는 묶음(bootstrap). 문장도 여기서 만든다 — 화면은 그리기만 한다."""
from __future__ import annotations

import json
from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, db, scoring

KST = ZoneInfo("Asia/Seoul")
KIND_KO = {"NEW": "신규편입", "EXIT": "편출", "UP": "확대", "DOWN": "축소"}
EOK = 1e8


def _second_thursday(y: int, m: int) -> Date:
    d = Date(y, m, 1)
    d += timedelta(days=(3 - d.weekday()) % 7)
    return d + timedelta(days=7)


def index_events(today: Date, months: int = 7) -> list[dict]:
    """코스피200·코스닥150 정기변경: 6·12월 선물만기일(둘째 목요일) 다음 영업일 — 규칙으로 계산한 '예상일'."""
    out, y, m = [], today.year, today.month
    for _ in range(months):
        if m in (6, 12):
            d = _second_thursday(y, m) + timedelta(days=1)
            if d >= today:
                out.append({"date": d.isoformat(), "t": "코스피200·코스닥150 정기변경", "s": "지수 정기변경",
                            "note": "규칙으로 계산한 예상일입니다. 거래소 공지로 확인하세요.", "x": []})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _manual_events() -> list[dict]:
    p = config.DATA_DIR / "calendar.json"  # 직접 추가하는 일정: [{"date","t","s","note","x":[]}]
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except ValueError:
        return []


def _why(s: dict) -> tuple[str, list[str]]:
    n_up, n_dn, act = s["n_up"], s["n_down"], s["n_act_moves"]
    amt = abs(s["net"]) / EOK
    if s["kind"] == "NEW":
        one = f"ETF {s['n_new']}곳이 새로 담기 시작했습니다."
    elif s["kind"] == "EXIT":
        one = f"ETF {s['n_exit']}곳이 전부 팔고 나갔습니다."
    elif s["kind"] == "UP":
        one = f"ETF {n_up}곳이 주식수를 늘렸습니다." if n_up > 1 else "ETF 1곳이 주식수를 늘렸습니다."
    elif s["kind"] == "DOWN":
        one = f"ETF {n_dn}곳이 주식수를 줄였습니다." if n_dn > 1 else "ETF 1곳이 주식수를 줄였습니다."
    else:
        return "최근 기준일에는 변화가 없습니다.", [
            f"ETF들이 가진 물량이 평소 거래량의 {s['B']}일치입니다. ETF가 움직이면 주가 영향이 큽니다."]
    whys = [f"추정 {'매수' if s['net'] > 0 else '매도'} 규모는 약 {amt:,.0f}억 원, 평소 하루 거래대금의 {s['str']}배입니다."]
    if act:
        whys.append(f"그중 {act}곳은 운용역이 직접 고르는 액티브 ETF입니다.")
    if abs(s["streak"]) >= 3:
        whys.append(f"{abs(s['streak'])}거래일 연속 {'늘리는' if s['streak'] > 0 else '줄이는'} 중입니다.")
    whys.append(f"ETF 보유 물량이 평소 거래량의 {s['B']}일치라 ETF 매매에 민감한 종목입니다.")
    return one, whys


def bootstrap() -> dict:
    now = datetime.now(KST)
    with db.session() as con:
        d = db.get_meta(con, "computed_date")
        status = {"lastRun": db.get_meta(con, "last_run"), "ok": db.get_meta(con, "last_ok", "1") == "1",
                  "msg": db.get_meta(con, "last_msg", "")}
        base = {"sample": False, "generated": now.isoformat(timespec="seconds"), "status": status,
                "events": sorted(_manual_events() + index_events(now.date()), key=lambda e: e["date"])}
        if not d:
            return {**base, "asof": None, "stocks": {}, "signals": {"up": [], "down": []}, "rank": [], "themes": [], "watch": [],
                    "picks": [], "pickMode": "impact", "hotEtfs": [], "trades": [], "tradeCount": 0}

        # 비교할 전일이 있는가 — 구성종목 날짜로 본다(집계 테이블은 계산이 돈 날에만 생긴다)
        prev_d = con.execute("SELECT MAX(date) FROM holding WHERE date<?", (d,)).fetchone()[0]
        hot = set((db.get_meta(con, "hot_etfs", "") or "").split(","))
        etf = {r["code"]: dict(r) for r in con.execute("SELECT * FROM etf")}
        names = {r["code"]: r["name"] for r in con.execute(
            "SELECT code, name FROM stock_daily WHERE name IS NOT NULL GROUP BY code")}
        mcap = {r["code"]: r["marcap"] for r in con.execute(
            "SELECT code, marcap FROM stock_daily WHERE marcap>0 GROUP BY code HAVING date=MAX(date)")}
        avg_amt = {r[0]: r[1] for r in con.execute(
            """SELECT code, AVG(amount) FROM (SELECT code, amount, ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) n
               FROM stock_daily WHERE amount>0) WHERE n<=20 GROUP BY code""")}
        agg = {r["code"]: dict(r) for r in con.execute("SELECT * FROM stock_agg WHERE date=?", (d,))}
        moves: dict[str, list] = {}
        for r in con.execute("SELECT * FROM change WHERE date=?", (d,)):
            moves.setdefault(r["stock_code"], []).append(dict(r))
        watch = [r[0] for r in con.execute("SELECT code FROM watch")]

        rank = sorted(agg, key=lambda k: -agg[k]["score"])
        # 화면 기본값이 리츠·시총 10조 초과를 빼므로, 걸러진 뒤에도 목록이 충분하도록 넉넉히 보낸다
        detail_codes = set(rank[:250]) | (set(moves) & set(agg)) | (set(watch) & set(agg))
        stocks = {}
        for k in detail_codes:
            a, mv = agg[k], moves.get(k, [])
            net = sum(m["amount"] for m in mv)
            s = {"code": k, "name": names.get(k, k), "cap": round(mcap.get(k, 0) / EOK), "score": a["score"],
                 "A": round(a["a"] * 100, 1), "B": round(a["b"], 1), "C": round(a["c"] * 100),
                 "etfN": a["etf_n"], "actN": a["act_n"], "net": net,
                 "str": round(abs(net) / avg_amt[k], 2) if avg_amt.get(k) else 0,
                 "n_new": sum(m["kind"] == "NEW" for m in mv), "n_exit": sum(m["kind"] == "EXIT" for m in mv),
                 "n_up": sum(m["amount"] > 0 for m in mv), "n_down": sum(m["amount"] < 0 for m in mv),
                 "n_act_moves": sum(bool(etf.get(m["etf_code"], {}).get("is_active")) for m in mv)}
            s["kind"] = (None if not mv or net == 0 else
                         ("NEW" if s["n_new"] and s["n_new"] == a["etf_n"] else "UP") if net > 0 else
                         ("EXIT" if s["n_exit"] and not s["n_up"] and s["n_exit"] == s["n_down"] else "DOWN"))
            hist = con.execute("SELECT date, held_shares, net FROM stock_agg WHERE code=? ORDER BY date DESC LIMIT 20", (k,)).fetchall()[::-1]
            s["hist"] = [{"d": h["date"], "v": round(h["held_shares"])} for h in hist]
            s["streak"] = scoring.streak([h["net"] or 0 for h in hist])
            s["px"] = [r["close"] for r in con.execute(
                "SELECT close FROM stock_daily WHERE code=? AND close>0 ORDER BY date DESC LIMIT 30", (k,)).fetchall()[::-1]]
            # 확신도(0~100): 몇 곳이 움직였나(곳당 10, 최대 40) + 평소 거래 대비 규모(1배=40, 최대 40) + 액티브(곳당 10, 최대 20)
            s["conv"] = round(min(40, 10 * len(mv)) + min(40, 40 * s["str"]) + min(20, 10 * s["n_act_moves"])) if mv else 0
            s["div"] = bool(s["n_up"] and s["n_down"])  # 어떤 ETF는 사고 어떤 ETF는 판다
            by_move = {m["etf_code"]: m for m in mv}
            s["holders"] = sorted(
                [{"etf": h["etf_code"], "name": etf[h["etf_code"]]["name"], "active": bool(etf[h["etf_code"]]["is_active"]),
                  "hot": h["etf_code"] in hot, "w": h["weight"],
                  "kind": (by_move.get(h["etf_code"]) or {}).get("kind"), "pct": (by_move.get(h["etf_code"]) or {}).get("pct"),
                  "amt": (by_move.get(h["etf_code"]) or {}).get("amount")}
                 for h in con.execute("SELECT etf_code, weight FROM holding WHERE date=? AND stock_code=?", (d, k)) if h["etf_code"] in etf],
                key=lambda h: -h["w"])[:12]
            s["holders"] += [{"etf": m["etf_code"], "name": etf[m["etf_code"]]["name"], "active": bool(etf[m["etf_code"]]["is_active"]),
                              "hot": m["etf_code"] in hot, "w": 0, "kind": "EXIT", "pct": None, "amt": m["amount"]}
                             for m in mv if m["kind"] == "EXIT" and m["etf_code"] in etf]
            themes = [etf[h["etf"]]["theme"] for h in s["holders"] if etf[h["etf"]]["theme"] not in ("시장대표", "기타")]
            s["theme"] = max(set(themes), key=themes.count) if themes else "시장대표"
            s["tags"] = ([KIND_KO[s["kind"]]] if s["kind"] else []) + (["엇갈림"] if s["div"] else []) + \
                        ([f"액티브 {s['n_act_moves']}곳"] if s["n_act_moves"] else []) + \
                        ([f"{abs(s['streak'])}일 연속"] if abs(s["streak"]) >= 3 else []) + \
                        (["상한 근접(추정)"] if any(h["w"] >= config.CAP_WARN_WEIGHT and etf[h["etf"]]["theme"] != "시장대표" for h in s["holders"]) else [])
            s["why"], s["whys"] = _why(s)
            stocks[k] = s

        sig = [s for s in stocks.values() if s["kind"]]
        order = lambda s: -(s["conv"] * (0.5 + s["score"] / 100))
        themes_out = _themes(con, d, etf, stocks)
        up = [s["code"] for s in sorted(sig, key=order) if s["net"] > 0]
        # 오늘 주목할 3종목: 시그널이 있으면 확신도 높은 확대 종목, 아직 없으면(첫날) ETF 영향도가 가장 큰 종목.
        # 리츠·초대형주는 찾는 대상이 아니므로 뺀다(화면의 랭킹 기본값과 같은 기준).
        fits = lambda s: s["theme"] != "리츠·부동산" and s["cap"] <= 100000
        picks = [c for c in up if fits(stocks[c])][:3]
        pick_mode = "signal" if picks else "impact"
        if not picks:
            picks = [k for k in rank if k in stocks and fits(stocks[k]) and stocks[k]["theme"] not in ("시장대표", "배당·가치")][:3] \
                    or [k for k in rank if k in stocks and fits(stocks[k])][:3]
        daily = {r["code"]: dict(r) for r in con.execute("SELECT code, aum_eok, ret3m FROM etf_daily WHERE date=?", (d,))}
        hot_out = []
        for c in hot:
            if c not in etf or c not in daily:
                continue
            top = [r["stock_name"] for r in con.execute(
                "SELECT stock_name FROM holding WHERE date=? AND etf_code=? AND stock_code IS NOT NULL ORDER BY weight DESC LIMIT 3", (d, c))]
            hot_out.append({"code": c, "name": etf[c]["name"], "theme": etf[c]["theme"], "active": bool(etf[c]["is_active"]),
                            "aum": round(daily[c]["aum_eok"] or 0), "ret3m": daily[c]["ret3m"], "top": top})
        hot_out.sort(key=lambda e: -(e["ret3m"] or -999))
        # 매매 내역: 어느 ETF가 무슨 종목을 얼마나 사고팔았나(추정 금액 큰 순). 화면에서 ETF별로 다시 묶는다
        trades = [{"etf": m["etf_code"], "etfName": etf[m["etf_code"]]["name"], "active": bool(etf[m["etf_code"]]["is_active"]),
                   "hot": m["etf_code"] in hot, "theme": etf[m["etf_code"]]["theme"], "code": k, "name": names.get(k, m["stock_name"]),
                   "kind": m["kind"], "pct": m["pct"], "amt": m["amount"], "w0": round(m["w_prev"] or 0, 2), "w1": round(m["w_cur"] or 0, 2)}
                  for k, mv in moves.items() for m in mv if m["etf_code"] in etf]
        trades.sort(key=lambda t: -abs(t["amt"] or 0))
        counts: dict[str, list[int]] = {}
        for t in trades:
            counts.setdefault(t["etf"], [0, 0])[0 if (t["amt"] or 0) > 0 else 1] += 1
        for e in hot_out:
            e["buys"], e["sells"] = counts.get(e["code"], [0, 0])
    return {**base, "asof": d, "prev": prev_d, "stocks": stocks,
            "signals": {"up": up, "down": [s["code"] for s in sorted(sig, key=order) if s["net"] < 0]},
            "picks": picks, "pickMode": pick_mode, "trades": trades[:400], "tradeCount": len(trades),
            "rank": [k for k in rank if k in stocks], "themes": themes_out, "watch": watch, "hotEtfs": hot_out}


def etf_detail(code: str) -> dict | None:
    """ETF 하나: 무엇을 얼마나 담고 있나, 최근 기준일에 무엇을 사고팔았나."""
    with db.session() as con:
        e = con.execute("SELECT * FROM etf WHERE code=?", (code,)).fetchone()
        d = con.execute("SELECT MAX(date) FROM holding WHERE etf_code=?", (code,)).fetchone()[0]
        if not e or not d:
            return None
        daily = con.execute("SELECT * FROM etf_daily WHERE code=? AND date=?", (code, d)).fetchone()
        prev = con.execute("SELECT units_k FROM etf_daily WHERE code=? AND date<? ORDER BY date DESC LIMIT 1", (code, d)).fetchone()
        moves = {r["stock_name"]: dict(r) for r in con.execute("SELECT * FROM change WHERE etf_code=? AND date=?", (code, d))}
        rows = con.execute("SELECT stock_name, stock_code, shares_cu, weight, weight_est FROM holding WHERE etf_code=? AND date=? ORDER BY weight DESC, shares_cu DESC", (code, d)).fetchall()
        hold = [{"name": r["stock_name"], "code": r["stock_code"], "w": round(r["weight"], 2), "shares": r["shares_cu"],
                 "kind": (moves.get(r["stock_name"]) or {}).get("kind"), "pct": (moves.get(r["stock_name"]) or {}).get("pct"),
                 "amt": (moves.get(r["stock_name"]) or {}).get("amount")} for r in rows]
        gone = [{"name": m["stock_name"], "code": m["stock_code"], "w": 0, "shares": 0, "kind": "EXIT", "pct": None, "amt": m["amount"]}
                for m in moves.values() if m["kind"] == "EXIT"]
        flow = None
        if prev and prev["units_k"] and daily and daily["units_k"] and daily["nav"]:
            flow = round((daily["units_k"] - prev["units_k"]) * 1000 * daily["nav"] / EOK)
        hot = code in (db.get_meta(con, "hot_etfs", "") or "").split(",")
    return {"code": code, "name": e["name"], "theme": e["theme"], "active": bool(e["is_active"]), "hot": hot, "asof": d,
            "index": e["base_index"], "issuer": e["issuer"], "aum": round(daily["aum_eok"] or 0) if daily else 0,
            "ret3m": daily["ret3m"] if daily else None, "flow": flow, "est": any(r["weight_est"] for r in rows),
            "count": len(hold), "holdings": hold[:40] + gone}


_cache: dict = {}


def cached() -> dict:
    """묶음은 수집이 끝나거나 관심종목이 바뀔 때만 달라진다 — 그때 invalidate() 한다."""
    if "b" not in _cache:
        _cache["b"] = bootstrap()
    return _cache["b"]


def invalidate() -> None:
    _cache.clear()


def _themes(con, d: str, etf: dict, stocks: dict) -> list[dict]:
    rows = con.execute(
        """SELECT c.code, c.aum_eok, c.ret3m, c.units_k, c.nav,
                  (SELECT p.units_k FROM etf_daily p WHERE p.code=c.code AND p.date<c.date ORDER BY p.date DESC LIMIT 1) prev_units
           FROM etf_daily c WHERE c.date=?""", (d,)).fetchall()
    t: dict[str, dict] = {}
    for r in rows:
        e = etf.get(r["code"])
        if not e or e["theme"] in ("시장대표", "기타"):
            continue
        x = t.setdefault(e["theme"], {"n": e["theme"], "flow": 0.0, "aum": 0.0, "_ret": 0.0, "etfs": 0, "up": [], "down": [], "etfList": []})
        x["etfList"].append({"code": e["code"], "name": e["name"], "aum": round(r["aum_eok"] or 0), "ret3m": r["ret3m"], "active": bool(e["is_active"])})
        if r["prev_units"] and r["units_k"] and r["nav"]:
            x["flow"] += (r["units_k"] - r["prev_units"]) * 1000 * r["nav"] / EOK  # 새로 설정(환매)된 금액, 억 원
        x["aum"] += r["aum_eok"] or 0
        x["_ret"] += (r["ret3m"] or 0) * (r["aum_eok"] or 0)
        x["etfs"] += 1
    for s in stocks.values():
        if s["kind"] and s["theme"] in t:
            t[s["theme"]]["up" if s["net"] > 0 else "down"].append(s["code"])
    for x in t.values():  # 테마 안에서 ETF 영향이 큰 종목 — 히트맵 타일을 눌렀을 때 보여 준다
        x["top"] = [s["code"] for s in sorted((s for s in stocks.values() if s["theme"] == x["n"]), key=lambda s: -s["score"])[:5]]
    out = []
    for x in t.values():
        x["ret3m"] = round(x.pop("_ret") / x["aum"], 1) if x["aum"] else 0
        x["flow"], x["aum"] = round(x["flow"]), round(x["aum"])
        x["etfList"] = sorted(x["etfList"], key=lambda e: -e["aum"])[:8]
        out.append(x)
    return sorted(out, key=lambda x: (-x["flow"], -x["ret3m"]))
