# ReadHorizon 실행 이미지
#   docker build -t readhorizon .
#   docker run -p 8000:8000 -v readhorizon-data:/data --env-file .env readhorizon
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RH_DB_PATH=/data/readhorizon.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY readhorizon ./readhorizon

# root가 아닌 사용자로 실행하고, 데이터(SQLite)는 /data 볼륨에 둔다
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
USER app
VOLUME ["/data"]

EXPOSE 8000

# 프록시 환경 변수가 있어도 로컬 헬스 체크는 직접 연결한다
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/api/health', timeout=4)"]

CMD ["python", "-m", "readhorizon", "serve", "--host", "0.0.0.0", "--port", "8000"]
