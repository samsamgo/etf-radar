"""돌고 있는 서버에서 결과를 받아 정적 사이트(site/)를 만든다 — GitHub Pages 에 올릴 파일.

    python scripts/publish_site.py [서버 주소] [출력 폴더] [--live]

도커 exec 에 기대지 않는다: 서버 주소만 닿으면 어느 컴퓨터에서든 돌릴 수 있다.
--live 는 장중 값(live.json)만 다시 받는다 — 몇 분마다 돌릴 때.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)


def write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def etf_codes(b: dict) -> set[str]:
    """화면에서 누를 수 있는 ETF 전부."""
    codes = {e["code"] for e in b.get("hotEtfs", [])} | {t["etf"] for t in b.get("trades", [])}
    codes |= {e["code"] for t in b.get("themes", []) for e in t.get("etfList", [])}
    codes |= {h["etf"] for s in b.get("stocks", {}).values() for h in s.get("holders", [])}
    return codes


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = (args[0] if args else "http://127.0.0.1:3200").rstrip("/")
    out = Path(args[1]) if len(args) > 1 else ROOT / "site"
    write(out / "data" / "live.json", get(base, "/api/live"))
    if "--live" in sys.argv:
        print("live.json 갱신")
        return
    b = get(base, "/api/bootstrap")
    b["watch"], b["alerts"], b["collecting"] = [], False, False  # 공개 파일 — 개인 관심종목·서버 상태는 싣지 않는다
    shutil.copyfile(ROOT / "app" / "web" / "index.html", out / "index.html")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    write(out / "data" / "bootstrap.json", b)
    shutil.rmtree(out / "data" / "etf", ignore_errors=True)
    codes, n = sorted(etf_codes(b)), 0
    for c in codes:
        try:
            write(out / "data" / "etf" / f"{c}.json", get(base, f"/api/etf/{c}"))
            n += 1
        except Exception as ex:  # 하나가 빠져도 나머지는 올린다 — 화면은 '자료 없음'으로 보여 준다
            print("ETF", c, "건너뜀:", ex)
        time.sleep(0.01)
    print({"asof": b.get("asof"), "stocks": len(b.get("stocks", {})), "etfs": f"{n}/{len(codes)}", "out": str(out)})


if __name__ == "__main__":
    main()
