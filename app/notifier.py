"""알림 채널. 지금은 디스코드 웹훅 하나 — 주소가 없으면 조용히 건너뛴다(화면은 그대로 동작).

보내기만 하면 되므로 봇이 아니라 웹훅을 쓴다. 웹훅 주소는 그것만 알면 누구나 글을 쓸 수 있는 비밀값이다.
"""
from __future__ import annotations

import logging
from datetime import date

import requests

from . import config

log = logging.getLogger("notifier")
LIMIT = 2000  # 디스코드 메시지 한 통의 글자 수 한도


def enabled() -> bool:
    return bool(config.DISCORD_WEBHOOK_URL)


def send(text: str) -> bool:
    if not enabled():
        log.info("디스코드 미설정 — 보내지 않음: %s", text.splitlines()[0] if text else "")
        return False
    try:
        r = requests.post(config.DISCORD_WEBHOOK_URL, json={"content": text[:LIMIT], "username": "ETF 매집 레이더"}, timeout=15)
    except requests.RequestException as ex:
        log.error("디스코드 전송 실패: %s", type(ex).__name__)  # 예외 문구에 웹훅 주소가 들어갈 수 있어 남기지 않는다
        return False
    if not r.ok:
        log.error("디스코드 전송 실패 %s: %s", r.status_code, r.text[:200])
    return r.ok


def link(path: str = "") -> str:
    # <주소> 로 감싸면 디스코드가 미리보기 카드를 붙이지 않는다
    return f"\n화면에서 보기: <{config.PUBLIC_URL}{path}>" if config.PUBLIC_URL else ""


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def daily_summary(b: dict) -> str | None:
    """새 기준일이 잡히면 한 통. 3~4줄로 끝낸다 — 자세한 건 화면에서."""
    if not b.get("asof"):
        return None
    st = b["stocks"]
    names = lambda codes: ", ".join(f"{st[c]['name']}({st[c]['tags'][0]})" for c in codes[:5]) or "없음"
    up, down = b["signals"]["up"], b["signals"]["down"]
    head = f"**ETF 매집 레이더** · {b['asof']} 기준"
    if not up and not down:
        why = "ETF들의 주식수 변화가 없었습니다." if b.get("prev") else "비교할 전일 데이터를 쌓는 중입니다. 시그널은 내일부터 나옵니다."
        return f"{head}\n{why}" + link()
    msg = f"{head}\n▲ 늘린 종목 {len(up)}개: {names(up)}\n▼ 줄인 종목 {len(down)}개: {names(down)}"
    inflow = [t for t in b["themes"] if t["flow"] > 0][:3]
    if inflow:
        msg += "\n돈 들어온 테마: " + ", ".join(f"{t['n']} +{t['flow']:,}억" for t in inflow)
    today = b["generated"][:10]
    soon = [e for e in b["events"] if 0 <= _days(e["date"], today) <= 3]
    if soon:
        msg += "\n다가오는 일정: " + ", ".join(f"{e['t']}(D-{_days(e['date'], today)})" for e in soon)
    return msg + link()


def failure(msg: str) -> str:
    return f"**ETF 매집 레이더 · 수집 실패**\n{msg}\n데이터가 하루 비면 시그널이 끊깁니다. 서버 로그를 확인하세요."
