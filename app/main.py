"""서버 본체: 화면 + API + 예약 작업을 한 프로세스에서 돌린다."""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import build, collector, config, db, intraday, notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")
WEB = Path(__file__).parent / "web"
_collecting = threading.Lock()


def collect_job() -> None:
    if not _collecting.acquire(blocking=False):
        return  # 이미 도는 중
    try:
        res = collector.run()
        build.invalidate()
        if not res["ok"]:
            notifier.send(notifier.failure(res["msg"]))
            return
        b = build.cached()
        with db.session() as con:  # 새 기준일이 처음 잡힌 실행에서만 요약을 보낸다
            if b.get("asof") and db.get_meta(con, "notified_date") != b["asof"]:
                msg = notifier.daily_summary(b)
                if msg and notifier.send(msg):
                    db.set_meta(con, "notified_date", b["asof"])
    finally:
        _collecting.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect().close()
    sched = BackgroundScheduler(timezone="Asia/Seoul")
    for t in config.COLLECT_TIMES.split(","):
        h, m = t.strip().split(":")
        sched.add_job(collect_job, "cron", day_of_week="mon-fri", hour=int(h), minute=int(m), misfire_grace_time=3600)
    sched.add_job(intraday.tick, "interval", seconds=config.INTRADAY_SECONDS, max_instances=1, coalesce=True)
    sched.start()
    with db.session() as con:
        empty = con.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 0
    if empty:  # 처음 켠 서버는 바로 한 번 모은다 — 하루라도 빨리 쌓여야 비교가 시작된다
        threading.Thread(target=collect_job, daemon=True).start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="ETF 매집 레이더", lifespan=lifespan, docs_url=None, redoc_url=None)


def _token() -> str:
    return hmac.new(config.APP_PASSWORD.encode(), b"etf-radar-session", hashlib.sha256).hexdigest()


@app.middleware("http")
async def auth(request: Request, call_next):
    open_paths = ("/login", "/health")
    if config.APP_PASSWORD and request.url.path not in open_paths:
        if not hmac.compare_digest(request.cookies.get("s", ""), _token()):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
            return RedirectResponse("/login")
    return await call_next(request)


LOGIN = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF 매집 레이더</title><body style="font-family:sans-serif;display:grid;place-items:center;min-height:90vh;margin:0 16px">
<form method="post" style="display:grid;gap:12px;width:100%;max-width:320px"><h1 style="font-size:22px;margin:0">ETF 매집 레이더</h1>
<label for="pw">비밀번호</label><input id="pw" name="password" type="password" autofocus style="font-size:18px;padding:12px">
<button style="font-size:18px;padding:12px">들어가기</button>__MSG__</form>"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return LOGIN.replace("__MSG__", "")


@app.post("/login")
def login(password: str = Form("")):
    if not config.APP_PASSWORD or not hmac.compare_digest(password, config.APP_PASSWORD):
        return HTMLResponse(LOGIN.replace("__MSG__", '<p style="color:#c00">비밀번호가 맞지 않습니다.</p>'), status_code=401)
    res = RedirectResponse("/", status_code=303)
    res.set_cookie("s", _token(), max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax",
                   secure=config.PUBLIC_URL.startswith("https"))
    return res


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/bootstrap")
def api_bootstrap():
    b = build.cached()
    return {**b, "collecting": _collecting.locked(), "alerts": notifier.enabled()}


@app.get("/api/etf/{code}")
def api_etf(code: str):
    if not (code.isalnum() and len(code) == 6):
        return JSONResponse({"error": "ETF 코드가 올바르지 않습니다"}, status_code=400)
    return build.etf_detail(code) or JSONResponse({"error": "그 ETF의 자료가 없습니다"}, status_code=404)


@app.get("/api/live")
def api_live():
    return intraday.snapshot()


@app.post("/api/watch/{code}")
def api_watch_add(code: str):
    if not (code.isalnum() and len(code) == 6):
        return JSONResponse({"error": "종목코드가 올바르지 않습니다"}, status_code=400)
    intraday.watch_add(code)
    build.invalidate()
    return {"watch": build.cached()["watch"]}


@app.delete("/api/watch/{code}")
def api_watch_remove(code: str):
    intraday.watch_remove(code)
    build.invalidate()
    return {"watch": build.cached()["watch"]}


@app.post("/api/collect")
def api_collect():
    if _collecting.locked():
        return {"started": False, "msg": "이미 수집 중입니다"}
    threading.Thread(target=collect_job, daemon=True).start()
    return {"started": True}
