FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Seoul ETF_DATA_DIR=/data
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
VOLUME /data
EXPOSE 3200

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:3200/health').status==200 else 1)"

# 예약 작업이 프로세스 안에서 돌기 때문에 워커는 반드시 하나
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3200", "--workers", "1"]
