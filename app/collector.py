"""하루 한 번 도는 수집·집계. 같은 날 여러 번 돌려도 결과가 같다(INSERT OR REPLACE)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, db, scoring, sources
from .themes import theme_of

log = logging.getLogger("collector")
KST = ZoneInfo("Asia/Seoul")


def today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def collect(limit: int | None = None) -> dict:
    """ETF 목록 → 종목 스냅샷 → ETF별 구성종목. limit 은 시험용."""
    etfs = [e for e in sources.etf_list() if scoring.is_plain_equity(e)]
    etfs.sort(key=lambda e: -e["aum_eok"])
    if limit:
        etfs = etfs[:limit]
    stocks = sources.stock_listing()
    code_of = {s["name"]: s["code"] for s in stocks}
    close_of = {s["name"]: s["close"] for s in stocks}
    now, ok, failed, rebuilt = today(), 0, [], []

    with db.session() as con:
        con.executemany("INSERT OR REPLACE INTO stock_daily VALUES(?,?,?,?,?,?,?)",
                        [(now, s["code"], s["name"], s["market"], s["close"], s["amount"], s["marcap"]) for s in stocks])

    for e in etfs:
        try:
            page, raw = sources.etf_page(e["code"])
            if not page["holdings"]:
                raise sources.SourceError("구성종목이 비어 있습니다")
        except Exception as ex:  # 한 곳이 깨져도 나머지는 계속
            failed.append(e["code"])
            log.warning("ETF %s %s 수집 실패: %s", e["code"], e["name"], ex)
            time.sleep(config.REQUEST_SLEEP)
            continue
        # 자료원이 비중을 전부 0으로 주는 ETF가 있다(실측: 국내 테마 ETF 1개). 그대로 두면 그 ETF는 경고 없이 계산에서 빠진다
        fixed = scoring.rebuild_weights([{**h, "close": close_of.get(h["name"])} for h in page["holdings"]])
        if fixed:
            page["holdings"], est = fixed, 1
            rebuilt.append(e["name"])
        else:
            est = 0
        with db.session() as con:
            con.execute("""INSERT OR REPLACE INTO etf(code,name,tab,theme,is_active,base_index,issuer,updated,type)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (e["code"], e["name"], e["tab"], theme_of(e["code"], e["name"]),
                         int("액티브" in e["name"] or "액티브" in page["type"]), page["base_index"], page["issuer"], now, page["type"]))
            con.execute("INSERT OR REPLACE INTO etf_daily VALUES(?,?,?,?,?,?,?)",
                        (page["date"], e["code"], e["price"], e["nav"], e["aum_eok"], e["ret3m"], page["units_k"]))
            con.execute("DELETE FROM holding WHERE date=? AND etf_code=?", (page["date"], e["code"]))
            con.executemany("""INSERT OR REPLACE INTO holding(date,etf_code,stock_name,stock_code,shares_cu,weight,weight_est)
                               VALUES(?,?,?,?,?,?,?)""",
                            [(page["date"], e["code"], h["name"], code_of.get(h["name"]), h["shares"], h["weight"], est)
                             for h in page["holdings"]])
        ok += 1
        time.sleep(config.REQUEST_SLEEP)

    if failed and len(failed) > 0.2 * len(etfs):
        raise sources.SourceError(f"ETF {len(etfs)}개 중 {len(failed)}개 수집 실패 — 사이트 구조가 바뀐 것 같습니다")
    if rebuilt:
        log.warning("비중을 주식수×종가로 복원한 ETF %d개: %s", len(rebuilt), ", ".join(rebuilt))
    return {"etfs": ok, "failed": failed, "stocks": len(stocks), "rebuilt": rebuilt}


def backfill_amounts(days: int = 45) -> int:
    """20일 평균 거래대금을 첫날부터 쓰기 위해, ETF가 담은 종목의 과거 거래대금을 한 번 채운다."""
    start = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.session() as con:
        codes = [r[0] for r in con.execute(
            """SELECT DISTINCT h.stock_code FROM holding h WHERE h.stock_code IS NOT NULL AND
               (SELECT COUNT(*) FROM stock_daily s WHERE s.code=h.stock_code) < 15""")]
    n = 0
    for code in codes:
        try:
            rows = sources.amount_history(code, start)
        except Exception as ex:
            log.warning("거래대금 보충 실패 %s: %s", code, ex)
            continue
        with db.session() as con:
            con.executemany("INSERT OR IGNORE INTO stock_daily(date,code,close,amount) VALUES(?,?,?,?)",
                            [(d, code, c, a) for d, c, a in rows])
        n += 1
        time.sleep(0.2)
    return n


def _holdings(con, etf_code: str, date: str) -> dict:
    return {r["stock_name"]: {"shares": r["shares_cu"], "weight": r["weight"], "code": r["stock_code"]}
            for r in con.execute("SELECT * FROM holding WHERE etf_code=? AND date=?", (etf_code, date))}


def compute(date: str | None = None) -> str | None:
    """기준일 date 의 변화(change)와 종목 요약(stock_agg)을 다시 계산한다."""
    with db.session() as con:
        date = date or con.execute("SELECT MAX(date) FROM holding").fetchone()[0]
        if not date:
            return None
        # 테마 규칙을 고치면 다시 수집하지 않아도 반영되게, 계산할 때마다 이름으로 다시 분류한다
        con.executemany("UPDATE etf SET theme=? WHERE code=?",
                        [(theme_of(r["code"], r["name"]), r["code"]) for r in con.execute("SELECT code, name FROM etf").fetchall()])
        etf_rows = [dict(r) for r in con.execute(
            """SELECT e.code, e.name, e.tab, e.type, e.is_active, d.aum_eok, d.ret3m,
                      (SELECT COALESCE(SUM(h.weight),0) FROM holding h
                        WHERE h.etf_code=e.code AND h.date=d.date AND h.stock_code IS NOT NULL) equity_w
               FROM etf e JOIN etf_daily d ON d.code=e.code AND d.date=?""", (date,))]
        # 채권·혼합·해외 ETF는 '잘나가는 ETF'로도, 종목 보유 합계로도 세지 않는다
        skipped = [e["name"] for e in etf_rows if not scoring.holds_domestic_stocks(e)]
        etf_rows = [e for e in etf_rows if scoring.holds_domestic_stocks(e)]
        if skipped:
            log.info("국내 주식형이 아니라 제외한 ETF %d개: %s", len(skipped), ", ".join(skipped))
        hot = scoring.pick_hot(etf_rows, config.MIN_AUM_EOK, config.HOT_RET_TOP)

        con.execute("DELETE FROM change WHERE date=?", (date,))
        for e in etf_rows:
            prev_date = con.execute("SELECT MAX(date) FROM holding WHERE etf_code=? AND date<?",
                                    (e["code"], date)).fetchone()[0]
            if not prev_date:
                continue
            prev, cur = _holdings(con, e["code"], prev_date), _holdings(con, e["code"], date)
            changes = scoring.classify_changes(prev, cur, config.CHANGE_TOL, config.MIN_SIGNAL_WEIGHT)
            if changes is None:
                log.info("%s %s: 바스켓 통째 조정 — 매매로 보지 않음", e["code"], e["name"])
                continue
            aum_prev = (con.execute("SELECT aum_eok FROM etf_daily WHERE code=? AND date=?",
                                    (e["code"], prev_date)).fetchone() or [e["aum_eok"]])[0] * 1e8
            for c in changes:
                code = (cur.get(c["name"]) or prev.get(c["name"]))["code"]
                if not code:
                    continue  # 현금·선물·해외 종목
                con.execute("INSERT OR REPLACE INTO change VALUES(?,?,?,?,?,?,?,?,?)",
                            (date, e["code"], code, c["name"], c["kind"], c["pct"], c["w_prev"], c["w_cur"],
                             scoring.est_amount(c, aum_prev, e["aum_eok"] * 1e8)))

        held, hot_held, etf_n, act_n = {}, {}, {}, {}
        by_etf = {e["code"]: e for e in etf_rows}
        for r in con.execute("SELECT etf_code, stock_code, weight FROM holding WHERE date=? AND stock_code IS NOT NULL", (date,)):
            e = by_etf.get(r["etf_code"])
            if not e:
                continue
            v = e["aum_eok"] * 1e8 * r["weight"] / 100
            k = r["stock_code"]
            held[k] = held.get(k, 0) + v
            etf_n[k] = etf_n.get(k, 0) + 1
            act_n[k] = act_n.get(k, 0) + e["is_active"]
            if e["code"] in hot:
                hot_held[k] = hot_held.get(k, 0) + v

        A, B, C, close = {}, {}, {}, {}
        for k, v in held.items():
            if v <= 0:
                continue  # 비중이 0.00% 로 반올림된 종목뿐 — ETF 영향이 없다
            s = con.execute("SELECT close, marcap FROM stock_daily WHERE code=? AND marcap>0 ORDER BY date DESC LIMIT 1", (k,)).fetchone()
            avg = con.execute("SELECT AVG(amount) FROM (SELECT amount FROM stock_daily WHERE code=? AND amount>0 ORDER BY date DESC LIMIT 20)", (k,)).fetchone()[0]
            if not s or not avg or avg < config.MIN_AVG_AMOUNT:
                continue  # 거래가 너무 없으면 점수가 높아도 쓸 수 없다
            A[k], B[k], C[k], close[k] = v / s["marcap"], v / avg, hot_held.get(k, 0) / v, s["close"]
        score = scoring.impact_scores(A, B, C)
        net = {r[0]: r[1] for r in con.execute("SELECT stock_code, SUM(amount) FROM change WHERE date=? GROUP BY 1", (date,))}

        con.execute("DELETE FROM stock_agg WHERE date=?", (date,))
        con.executemany("INSERT INTO stock_agg VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(date, k, held[k], hot_held.get(k, 0), etf_n[k], act_n[k], A[k], B[k], C[k], score[k],
                          net.get(k, 0), held[k] / close[k] if close[k] else 0) for k in A])
        db.set_meta(con, "hot_etfs", ",".join(sorted(hot)))
        db.set_meta(con, "computed_date", date)
    return date


def run(limit: int | None = None) -> dict:
    started = datetime.now(KST).isoformat(timespec="seconds")
    try:
        res = collect(limit)
        # 과거치가 모자란 종목만 골라 채우므로 매번 불러도 된다(다 찼으면 아무 일도 하지 않는다).
        # 새로 편입된 종목은 언제든 생기고, 하루치 평균으로 계산한 '며칠치 거래량'은 믿을 수 없다.
        res["backfilled"] = backfill_amounts()
        res["date"] = compute()
        res["ok"], res["msg"] = True, f"ETF {res['etfs']}개 수집"
    except Exception as ex:
        log.exception("수집 실패")
        res = {"ok": False, "msg": str(ex)}
    with db.session() as con:
        db.set_meta(con, "last_run", started)
        db.set_meta(con, "last_ok", int(res["ok"]))
        db.set_meta(con, "last_msg", res["msg"])
    return res


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run(int(sys.argv[1]) if len(sys.argv) > 1 else None))
