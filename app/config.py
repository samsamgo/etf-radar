"""설정은 전부 환경변수로 받는다. 비밀값은 코드·저장소에 두지 않는다."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ETF_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "etf-radar.db"
RAW_DIR = DATA_DIR / "raw"  # 파서가 깨졌을 때 원인을 볼 수 있게 원본을 남긴다

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")  # 비밀값 — 이 주소만 알면 누구나 채널에 글을 쓴다
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # 비우면 로그인 없이 열린다(내부망 전용)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # 알림에 넣을 대시보드 주소

MIN_AUM_EOK = float(os.environ.get("MIN_AUM_EOK", 500))       # 핫 ETF: 순자산 하한(억)
HOT_RET_TOP = float(os.environ.get("HOT_RET_TOP", 0.30))      # 핫 ETF: 3개월 수익률 상위 비율
MIN_AVG_AMOUNT = float(os.environ.get("MIN_AVG_AMOUNT", 5e8))  # 하루 거래대금이 이보다 작으면 랭킹 제외(원)
MIN_SIGNAL_WEIGHT = float(os.environ.get("MIN_SIGNAL_WEIGHT", 0.3))
CHANGE_TOL = float(os.environ.get("CHANGE_TOL", 0.005))
CAP_WARN_WEIGHT = float(os.environ.get("CAP_WARN_WEIGHT", 25))  # 이 비중 이상이면 '상한 근접' 추정 표시
REQUEST_SLEEP = float(os.environ.get("REQUEST_SLEEP", 0.7))   # 비공식 엔드포인트에 부담을 주지 않는다

COLLECT_TIMES = os.environ.get("COLLECT_TIMES", "08:20,18:30")  # 평일, 한국 시간
INTRADAY_SECONDS = int(os.environ.get("INTRADAY_SECONDS", 60))
INTRADAY_ALERT_PCT = float(os.environ.get("INTRADAY_ALERT_PCT", 5))
WATCH_LIMIT = int(os.environ.get("WATCH_LIMIT", 60))          # 장중에 시세를 따라갈 종목 수
# 장중 시장 수급(금융투자 순매수·프로그램 매매). 네이버 증권 내부 API 라 약관상 회색지대 — 0 으로 끌 수 있다
MARKET_FLOWS = os.environ.get("MARKET_FLOWS", "1") == "1"
