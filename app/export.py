"""정적 사이트 내보내기 — GitHub Pages 처럼 서버를 돌릴 수 없는 곳에 올릴 파일을 만든다.

화면(index.html)은 /api 가 없으면 ./data/*.json 을 읽는다. 여기서 그 파일들을 쓴다.
    python -m app.export <출력 폴더>          전부(화면 + 결과 + ETF 상세)
    python -m app.export <출력 폴더> --live   장중 값(live.json)만 — 자주 돌릴 때
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from . import build, db, intraday

WEB = Path(__file__).parent / "web"


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def export_live(out: Path) -> None:
    if not intraday.snapshot()["at"]:
        intraday.tick()  # 따로 띄운 프로세스라 서버의 메모리 값을 못 본다 — 한 번 받아 온다
    _write(out / "data" / "live.json", intraday.snapshot())


def export_all(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(WEB / "index.html", out / "index.html")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    b = build.bootstrap()
    b["watch"] = []  # 공개되는 파일 — 개인 관심종목은 싣지 않는다(화면이 각자 브라우저에 따로 저장한다)
    _write(out / "data" / "bootstrap.json", b)
    with db.session() as con:
        codes = [r[0] for r in con.execute("SELECT code FROM etf")]
    etf_dir = out / "data" / "etf"
    shutil.rmtree(etf_dir, ignore_errors=True)
    n = 0
    for code in codes:
        d = build.etf_detail(code)
        if d:
            _write(etf_dir / f"{code}.json", d)
            n += 1
    export_live(out)
    return {"asof": b.get("asof"), "stocks": len(b.get("stocks", {})), "etfs": n}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    target = Path(sys.argv[1])
    if "--live" in sys.argv:
        export_live(target)
        print("live.json 갱신")
    else:
        print(export_all(target))
