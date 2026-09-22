# ETF 매집 레이더

국내 주식형 ETF 400여 개의 구성종목을 매일 모아, **ETF들이 주식수를 늘리거나 줄인 개별 종목**을 찾아 준다.
개인 프로젝트.

> ## 🟢 지금 상태 (2026-09-22)
>
> | | |
> |---|---|
> | 서버 | 이 PC 도커 `etf-radar-app-1` · `http://127.0.0.1:3200` · 재부팅해도 자동 기동 |
> | 링크 | <https://samsamgo.github.io/etf-radar/> (GitHub Pages, 공개) |
> | 코드 | <https://github.com/samsamgo/etf-radar> (공개) |
> | 수집 | 평일 08:20 · 18:30 자동. 2026-09-22 08:54 에 정상 수집(ETF 411개) |
> | 시그널 | **나오기 시작했다.** 9/21 기준 확대 110 · 축소 117 · ETF 매매 354건 |
> | 알림 | 디스코드 서버 "ETF 레이더" `#일반` 웹훅 연결됨 |
>
> **링크는 자동으로 갱신되지 않는다.** 서버가 새로 모은 결과를 사이트에 올리려면 이 명령을 돌려야 한다.
>
> ```
> powershell -ExecutionPolicy Bypass -File C:\Users\dmast\Desktop\pro\etf-radar\scripts\publish.ps1
> ```
>
> 자동화하려면 이 명령을 작업 스케줄러에 등록하면 된다(아직 등록하지 않았다).
>
> **아직 안 한 것**
> - `APP_PASSWORD` 가 비어 있다. 지금은 `127.0.0.1` 로만 열려 있어 괜찮지만,
>   네트워크에 열거나 노트북으로 옮기기 전에 `secrets\etf-radar.env` 에 넣어야 한다.
> - 사이트 자동 갱신(작업 스케줄러 등록).
> - 시그널 적중률 검증(며칠치 쌓인 뒤에 할 일).

## 무엇을 보여 주나

| 화면 | 내용 |
|---|---|
| 오늘 | ETF가 늘린/줄인 종목 TOP5, 돈이 들어오는 테마, 관심종목, 다가오는 일정 |
| 시그널 | 신규편입 · 확대 · 축소 · 편출 — **비중이 아니라 1CU당 주식수 변화**로 판정(주가만 올라서 생기는 가짜 신호 제거) |
| 테마 | 테마별 ETF 자금 유입(상장좌수 증감 × NAV), 테마 안에서 늘린/줄인 종목 |
| 랭킹 | 영향도 = 지분 중 ETF 보유(A) + 며칠치 거래량인가(B) + 잘나가는 ETF 몫(C). 대형주는 저절로 아래로 간다 |
| 일정 | 코스피200·코스닥150 정기변경 예상일(규칙 계산) + 직접 추가한 일정 |

장중(평일 09:00~15:35)에는 시그널·관심 종목의 현재가와 핫 ETF의 등락·괴리율을 1분마다 갱신한다.
**구성종목 자체는 하루 한 번만 공시되므로 "누가 무엇을 샀나"는 장중에 바뀌지 않는다.**

## 켜기

```
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

화면: `http://<이 컴퓨터 IP>:3200` (같은 와이파이 안). 키가 하나도 없어도 동작한다.

### 첫날에 알아 둘 것

- 빈 DB로 켜면 바로 첫 수집을 한다(10~20분). 그동안 화면에 "모으는 중"이 나온다.
- **시그널은 이틀째부터 나온다.** 전일과 비교해야 하는데, 무료로 받을 수 있는 과거 구성종목이 없다.
- 그래서 `etf-data` 볼륨(= `/data/etf-radar.db`)이 이 프로젝트의 자산이다. **지우면 복구할 수 없다.**

### 다른 컴퓨터(서버 노트북)로 옮길 때 — 쌓인 데이터를 들고 간다

```
# 원래 컴퓨터: 볼륨에서 DB 꺼내기
docker compose stop
docker run --rm -v etf-radar_etf-data:/data -v "${PWD}:/out" alpine cp /data/etf-radar.db /out/etf-radar.db

# 새 컴퓨터: 프로젝트 폴더와 etf-radar.db 를 옮긴 뒤
docker compose build
docker volume create etf-radar_etf-data
docker run --rm -v etf-radar_etf-data:/data -v "${PWD}:/in" alpine cp /in/etf-radar.db /data/etf-radar.db
docker compose up -d
```

수집은 한 곳에서만 돌린다(두 곳에서 돌리면 같은 사이트를 두 번 긁는다).

## 링크로 보기 (GitHub Pages)

Pages 는 서버를 돌릴 수 없으므로, 돌고 있는 서버에서 결과를 받아 **정적 파일**로 올린다. 화면은 `/api` 가 없으면 `./data/*.json` 을 읽는다.

```
python scripts/publish_site.py http://127.0.0.1:3200 site          # 화면 + 결과 + ETF 상세
python scripts/publish_site.py http://127.0.0.1:3200 site --live   # 장중 값만
cd site && git init -q -b gh-pages && git add -A && git commit -qm "site" && git push -f <저장소 주소> gh-pages
```

올린 시점의 스냅샷이다(헤더에 "○○:○○ 기준"). 누구나 볼 수 있으므로 개인 관심종목은 파일에 싣지 않고, 화면이 각자 브라우저에 따로 저장한다.

## 설정 (전부 선택)

서버는 환경변수만 읽는다. 변수 목록은 `secrets/etf-radar.env.example`.
compose 는 `secrets/etf-radar.env` 가 있으면 읽고, 없으면 그냥 뜬다.

| 변수 | 없으면 |
|---|---|
| `DISCORD_WEBHOOK_URL` | 핸드폰 알림만 안 간다 |
| `APP_PASSWORD` | 로그인 없이 열린다 — 내부망 전용일 때만 |
| `PUBLIC_URL` | 알림에 화면 링크가 빠진다 |

> **비밀값은 이 프로젝트의 `secrets/etf-radar.env` 에 직접 둔다(2026-09-21 결정).**
> 웹훅 주소·비밀번호는 채팅·노션·git 에 쓰지 않는다(`secrets/*` 는 `.gitignore`).

알림은 디스코드 **웹훅**(채널 설정 → 연동 → 웹후크). 보내기만 하므로 봇 계정은 필요 없다.
새 기준일이 처음 잡힐 때 요약 한 통, 장중에 시그널·관심 종목이 ±5% 움직이면 종목당 하루 한 통, 수집이 실패하면 한 통.

조정값(기본): `MIN_AUM_EOK=500` `HOT_RET_TOP=0.30` `MIN_AVG_AMOUNT=5e8` `COLLECT_TIMES=08:20,18:30` `INTRADAY_ALERT_PCT=5`

일정 추가: 볼륨의 `/data/calendar.json` 에 `[{"date":"2026-10-08","t":"○○ 지수 정기변경","s":"방산","note":"…","x":[]}]`.
테마 지수의 정기변경일은 지수마다 달라 자동으로 받을 수 없다.

## 데이터 출처와 약한 고리

| 무엇 | 어디서 | 비고 |
|---|---|---|
| ETF 목록·가격·NAV·순자산·3개월 수익률 | 네이버 금융 ETF 목록 | 1회 호출 |
| 구성종목(1CU당 주식수·비중)·상장좌수 | WiseReport ETF 페이지 | ETF당 1회, 0.7초 간격. 종목코드가 없어 **종목명으로 매핑** |
| 전 종목 종가·거래대금·시가총액 | FinanceDataReader | 과거 거래대금은 종가×거래량 근사 |
| 장중 현재가 | 네이버 실시간 조회 | 키 불필요 |

- 전부 **비공식 엔드포인트**다. 모양이 바뀌면 수집이 실패하고, 실패하면 알림이 간다(`SourceError`).
- KRX 정보데이터시스템은 2025-12 부터 로그인제라 `pykrx` 의 ETF 함수는 계정 없이 동작하지 않는다(2026-09-21 실측).
- 매매 규모(억 원)는 `순자산 × 비중 × 주식수 변화율` 로 계산한 **추정치**다.
- '상한 근접'은 비중 25% 이상이면 붙이는 추정 표시다. 실제 상한은 지수마다 다르다.
- 3개월 수익률 상위 = 상대 순위. 하락장에서는 "덜 빠진 ETF"라는 뜻이 된다.

### 아직 없는 것

- 장중 프로그램 매매·외국인/기관 추정 수급: 찾은 출처가 모두 증권사 API(한국투자증권 Open API 등, 본인 계좌의 키 필요). 키 없이 테스트할 수 없어 넣지 않았다 — `app/intraday.py` 에 provider 로 붙일 자리.
- 외부(집 밖) 접속: 지금은 같은 네트워크 안에서만 열린다.
- 과거 구성종목 백필: 삼성 KODEX 는 운용사 사이트에서 과거 날짜 조회가 되는 것을 확인했다(운용사 내부 ID 매핑 필요). 다른 운용사는 미확인.

## 개발

```
python -m pytest tests          # 계산 로직(순수 함수) 테스트
python -m app.collector 15      # 순자산 상위 15개 ETF만 시험 수집
python -m app.collector         # 전체 수집 + 집계
uvicorn app.main:app --port 3200
```

`app/scoring.py` 는 DB·네트워크를 모르는 순수 함수만 둔다. 화면(`app/web/index.html`)은 `/api/bootstrap` 을 받아 그리기만 하고,
서버에 못 붙으면 가상의 예시 데이터로 시안처럼 동작한다(같은 파일을 시안으로도 쓴다).
