import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS etf(
  code TEXT PRIMARY KEY, name TEXT, tab INTEGER, theme TEXT, is_active INTEGER,
  base_index TEXT, issuer TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS etf_daily(  -- date = 구성종목 기준일
  date TEXT, code TEXT, price REAL, nav REAL, aum_eok REAL, ret3m REAL, units_k REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS holding(    -- 1CU당 주식수
  date TEXT, etf_code TEXT, stock_name TEXT, stock_code TEXT, shares_cu REAL, weight REAL,
  PRIMARY KEY(date, etf_code, stock_name));
CREATE INDEX IF NOT EXISTS ix_holding_stock ON holding(stock_code, date);
CREATE TABLE IF NOT EXISTS stock_daily(
  date TEXT, code TEXT, name TEXT, market TEXT, close REAL, amount REAL, marcap REAL,
  PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS ix_stock_daily_code ON stock_daily(code, date);  -- 종목별 가격 이력 조회
CREATE TABLE IF NOT EXISTS change(     -- ETF 한 곳이 종목 하나를 어떻게 바꿨나
  date TEXT, etf_code TEXT, stock_code TEXT, stock_name TEXT, kind TEXT, pct REAL,
  w_prev REAL, w_cur REAL, amount REAL,
  PRIMARY KEY(date, etf_code, stock_name));
CREATE INDEX IF NOT EXISTS ix_change_stock ON change(stock_code, date);
CREATE TABLE IF NOT EXISTS stock_agg(  -- 종목 하루 요약
  date TEXT, code TEXT, held REAL, hot_held REAL, etf_n INTEGER, act_n INTEGER,
  a REAL, b REAL, c REAL, score INTEGER, net REAL, held_shares REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS px_hist(   -- 수정주가 종가 1년치(신호 종목·시장지수 KS11/KQ11). 60일 주도주·52주 고가용
  code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS signal_log(  -- 레이더가 띄운 신호(실전 성적 기록용). side = up|down, edges = 근거 키 쉼표 목록
  date TEXT, code TEXT, side TEXT, kind TEXT, conv INTEGER, edges TEXT, close REAL, cap REAL, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS series(   -- 투자 계획용 외부 시리즈(S&P·금·환율·VIX·지수 ETF 종가, FinanceDataReader)
  key TEXT, date TEXT, value REAL, PRIMARY KEY(key, date));
CREATE TABLE IF NOT EXISTS fund(     -- 종목 PBR·BPS·EPS(네이버 종목 페이지, 월 1회). 저PBR 갈래·'PBR 하위 절반' 판정용
  code TEXT PRIMARY KEY, asof TEXT, pbr REAL, bps REAL, eps REAL, per REAL);
CREATE TABLE IF NOT EXISTS plan_log( -- 날짜별 투자 계획(strategy.compute 결과 JSON). 나중에 실제 성적을 되짚어 볼 근거
  date TEXT PRIMARY KEY, exec_date TEXT, json TEXT);
CREATE TABLE IF NOT EXISTS watch(code TEXT PRIMARY KEY, added TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


# 나중에 추가된 열. 쌓인 DB는 다시 만들 수 없으므로(과거 구성종목은 재수집 불가) 지우지 않고 열만 더한다.
MIGRATIONS = [
    ("etf", "type", "TEXT"),             # 자료원의 상품 분류: '국내주식형, 섹터' · '채권형' · '해외주식형' …
    ("holding", "weight_est", "INTEGER DEFAULT 0"),  # 1 = 자료원이 비중을 주지 않아 주식수×종가로 복원한 값
]


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        if col not in {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.commit()
    return con


@contextmanager
def session():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
