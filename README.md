# ReadHorizon

소설·시·희곡·수필·고전·논문·기사 같은 텍스트를 **원문에 근거해** 분석하고, 문맥·문단 흐름·의미·번역을 이해하도록 돕는 프로그램입니다.
수능 국어처럼 정답이 있는 독해에도 쓸 수 있도록, 모든 분석에 원문 인용을 붙이고 인용이 실제 원문과 맞는지 자동으로 확인합니다.

> 설계 배경과 결정 사항은 [docs/design.md](docs/design.md)에 정리되어 있습니다.

## 핵심 원칙

| 원칙 | 구현 |
|---|---|
| 근거 우선 | Claude API Citations로 문단(시는 행) 단위 인용을 받고, 인용문이 해당 원문에 실제로 있는지 대조합니다. 근거가 없으면 "원문에서 근거를 찾을 수 없습니다"라고 답합니다. |
| 사실/해석 구분 | 줄거리·화자·표현법은 사실로, 주제·상징은 '해석'으로 표시하고, 해석이 갈리면 여러 관점을 나란히 제시합니다. |
| 신뢰도 표시 | 출처 등급(원문 / A 공식·학술 / B 사전·평론 / C 참고)과 근거율, 신뢰도 배지를 화면에 표시합니다. |
| 개인정보 최소 수집 | 계정·이메일을 받지 않습니다. 민감정보는 저장·전송 전에 자동으로 가리고, 수집 항목은 화면에서 고지합니다. |
| 저작권 존중 | 서점 페이지를 크롤링하지 않습니다. 사용자 자료는 그 사용자의 자료실에서만 쓰고, 공용 DB나 AI 학습에 쓰지 않습니다. |

## 기능 (MVP)

- **카테고리별 분석 프리셋**
  - 문학: 소설 · 시 · 희곡 · 수필
  - 학습·연구: 수능 독서 · 수능 문학(현대·고전) · 논문 · 교재
  - 사용자 지정: 기사 · 강의 자료 · 일반 텍스트 (내 자료실 전용)
- **분석 종류**: 작품 분석 / 문단 흐름 / 번역·풀이(현대어 풀이·쉬운 말·영어·한국어) / 읽기 가이드
- **질의응답**: 원문 인용이 붙는 답변. 선지를 붙여넣으면 선지별 근거와 `정답: N`을 판단합니다.
- **입력**: TXT · MD · PDF · DOCX · HWPX 파일 업로드, 원문 붙여넣기, **책 소개 붙여넣기(선택)**
  - 원문 없이 소개글만 넣으면 **읽기 가이드만** 제공합니다(소개글은 C등급 `사용자 제공 소개`로 표시하며 해석 근거로 쓰지 않음).
- **서지 검색(선택)**: 국립중앙도서관 ISBN 서지정보 API. KDC 분류번호로 장르를 추천합니다(8X1 시, 8X2 희곡, 8X3 소설, 8X4 수필).
- **3단 뷰어**: 원문 | 분석 | 질의응답. 인용 표시를 누르면 원문의 해당 위치가 강조됩니다. 모바일에서는 탭으로 전환합니다.
- **개인정보**: 주민·외국인등록번호, 카드번호, 전화번호, 이메일 자동 마스킹. 최근 30일 마스킹 **항목·개수** 고지. 마지막 이용 후 180일(설정 가능)이 지나면 자동 삭제.
- **벤치마크**: 선다형 문항 정답률과 근거율을 측정합니다.

## 빠른 시작

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env          # ANTHROPIC_API_KEY를 넣으면 실제 분석, 비우면 데모 모드
python -m readhorizon serve   # http://127.0.0.1:8000
```

- **데모 모드**: API 키가 없으면 실제 분석 대신 원문 일부를 인용하는 고정 응답을 돌려줍니다. 화면 흐름과 인용·검증 동작을 확인하는 용도이며, 결과에 "데모"라고 표시됩니다.
- 샘플 작품(저작권 만료): `samples/works/서시_윤동주.txt`, `samples/works/진달래꽃_김소월.txt`

## 설정 (.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ANTHROPIC_API_KEY` | (없음) | Claude API 키. 없으면 데모 모드 |
| `RH_LLM_MODE` | 자동 | `live` / `demo` 강제 |
| `RH_MODEL` | `claude-opus-5` | 분석 모델 |
| `RH_EFFORT` | `high` | `low`–`max`. 비용을 줄이려면 `medium` |
| `RH_RETENTION_DAYS` | `180` | 마지막 이용 후 보관 일수 |
| `RH_NL_CERT_KEY` | (없음) | 국립중앙도서관 서지 API 인증키. 없으면 서지 검색 숨김 |
| `RH_DB_PATH` | `data/readhorizon.db` | SQLite 경로 (git에 올리지 않음) |
| `RH_MAX_UPLOAD_MB` | `20` | 파일·붙여넣기 원문의 최대 크기(요청 전체는 이보다 1MB 더 허용) |
| `RH_FULLTEXT_CHAR_LIMIT` | `200000` | 이보다 긴 작품은 관련 문단만 골라 분석(결과에 "발췌" 표시). 최소 5000 |
| `RH_LLM_PER_MINUTE` | `10` | 접속 IP별 분당 분석·질문 수(저장된 결과 조회는 제외). `0`이면 끔 |
| `RH_LLM_DAILY_LIMIT` | `1000` | 서버 프로세스 전체의 하루(UTC) 분석·질문 수. API 비용 상한 역할. `0`이면 끔 |
| `RH_CREATE_PER_MINUTE` | `30` | 접속 IP별 분당 작품 등록 수. `0`이면 끔 |
| `RH_ACCESS_LOG` | `0` | `1`이면 접속 로그(IP 포함)를 남김. 기본은 남기지 않음(개인정보 최소 수집) |

Claude API 호출에는 `fallbacks: "default"`(서버 측 대체 모델)가 켜져 있습니다. 안전 분류기가 요청을 거절하면 API가 권장 대체 모델로 다시 시도하며, 이때 결과에 "(대체 모델)"이 표시됩니다.

## 배포

### Docker

```bash
docker build -t readhorizon .
docker run -d --name readhorizon -p 8000:8000 \
  -v readhorizon-data:/data \
  --env-file .env \
  readhorizon
```

- root가 아닌 사용자(uid 10001)로 실행되며, SQLite 데이터는 `/data` 볼륨에 저장됩니다(`RH_DB_PATH=/data/readhorizon.db`).
- 이미지에 헬스 체크(`/api/health`)가 들어 있습니다.
- `.env`는 이미지에 포함되지 않습니다(`.dockerignore`). 키는 `--env-file`이나 배포 플랫폼의 비밀 값으로 넘기세요.
- 참고자료 가져오기: `docker cp refs.json readhorizon:/tmp/ && docker exec readhorizon python -m readhorizon import-references /tmp/refs.json` (재시작 불필요)

### 리버스 프록시(HTTPS) 뒤에서 운영할 때

- **HTTPS**로만 공개하세요(nginx·Caddy·클라우드 로드밸런서 등).
- 분석은 1~2분 걸릴 수 있습니다. 프록시 **응답 대기 시간을 300초 이상**으로 늘리세요(nginx 기본 60초에서는 504가 납니다). 예: `proxy_read_timeout 300s;`
- 업로드 크기: 프록시의 본문 크기 제한을 `RH_MAX_UPLOAD_MB`보다 1MB 이상 크게 두세요. 예: `client_max_body_size 21m;`
- 호출 제한이 실제 접속 IP로 동작하도록 프록시 주소를 `FORWARDED_ALLOW_IPS` 환경 변수로 지정하세요(uvicorn 설정). 예: `-e FORWARDED_ALLOW_IPS=10.0.0.5`
- 서버는 프로세스 1개로 실행됩니다. 호출 제한은 프로세스 메모리에서 세므로, 서버를 여러 대로 늘릴 때는 프록시·게이트웨이에서도 제한을 두세요.

### API로 작품을 등록할 때

`POST /api/works`는 `multipart/form-data`로 보내세요(웹 화면의 FormData와 같은 방식).
`application/x-www-form-urlencoded`는 한글이 약 3배로 부풀어 요청 크기 제한에 먼저 걸립니다.

### CI

`.github/workflows/ci.yml`이 Python 3.10–3.13 테스트·린트와 Docker 이미지 빌드·실행 확인을 합니다.

## 공용 참고자료 가져오기

해석의 2차 근거가 되는 공용 참고자료는 **퍼블릭 도메인이거나 이용 허락을 받은 자료만** 넣습니다. 출처·등급·라이선스는 필수 항목입니다.

```bash
python -m readhorizon import-references samples/references.sample.json
```

```json
[{"title": "…", "source": "출처", "tier": "A|B|C", "license": "이용 조건", "work_title": "연결할 작품 제목(선택)", "url": "…", "text": "본문"}]
```

- `work_title`이 있는 자료는 제목이 같은 작품에만 인용됩니다.
- 서버가 실행 중일 때 가져온 자료도 다음 검색부터 바로 반영됩니다.

## 벤치마크 (정답률 · 근거율)

```bash
python -m readhorizon eval samples/eval_items.sample.json
```

- 샘플 문항은 **실제 기출이 아닌 예시**입니다. 실제 기출은 한국교육과정평가원 이용 조건을 확인한 뒤 따로 준비하세요(저장소에 커밋하지 않는 것을 권장).
- 결과: `accuracy`(정답률), `mean_grounding_ratio`(평균 근거율), 문항별 예측·검증 실패 인용 수

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q
ruff check readhorizon tests
```

테스트는 파서, 정규화(NFC·긴 문단 분할), 마스킹, 검색, 서지 파싱, 인용 검증, API, 자료실 분리, 보관 기간 삭제, 요청 크기·호출 제한, XML 엔티티 공격 차단을 다룹니다.
또 실제 Anthropic SDK로 로컬 가짜 서버와 통신해 요청 형식(Citations, 캐시, fallbacks)과 스트리밍 응답 파싱을 확인합니다.

## 구조

```
readhorizon/
  main.py              FastAPI 앱 (REST API + 웹 화면)
  config.py            환경 설정
  db.py                SQLite 저장소 (자료실 분리, 보관 기간, 마스킹 기록)
  trust.py             출처 등급 · 신뢰도 배지
  evaluation.py        벤치마크
  cli.py               serve / import-references / purge / eval
  ratelimit.py         접속 IP별·일일 호출 제한
  ingest/              파일 파싱(PDF·DOCX·HWPX·TXT·MD), 문단·행 정규화
  analysis/            프리셋, 프롬프트, Claude 호출, 인용 검증, 분석 엔진
  references/          공용 참고자료 가져오기 · BM25 검색(한글 2-gram, 옛한글 포함)
  catalog/             국립중앙도서관 서지 API, KDC 장르 추천
  privacy/             민감정보 마스킹, 수집 항목 고지
  static/              웹 화면 (HTML/CSS/JS, 외부 CDN 없음, CSP 적용)
samples/               저작권 만료 작품, 참고자료·문항 형식 예시
tests/
docs/design.md         설계 결정 기록
Dockerfile             실행 이미지 (non-root, /data 볼륨, 헬스 체크)
.github/workflows/     CI (테스트·린트·Docker)
```

## 로드맵

| 단계 | 내용 |
|---|---|
| 1단계 (현재) | 파일·붙여넣기 입력, 카테고리 프리셋, 근거 인용 분석·질의응답, 신뢰도 배지, 소개글 읽기 가이드, 서지 API, 마스킹·보관 기간 |
| 2단계 | 이미지·스캔 PDF 입력(OCR, 옛한글 검증), HWP 5.0 바이너리, 문장 단위 인용, 고전 주석 사전 연동 |
| 3단계 | 계정 기반 자료실(인증), 외부 연동(Drive·Notion, 최소 권한), 논문 인용 네트워크, PostgreSQL + pgvector |
| 4단계 | 벤치마크·사용자 피드백이 쌓이면 해설 형식 파인튜닝 검토 |

## 운영 전 확인할 것

- 법률 검토: 사용자 업로드 원문 처리 범위, 소개글 붙여넣기, 14세 미만 이용자(법정대리인 동의) 정책
- 자료실 ID는 브라우저에 저장된 무작위 값이라 인증이 아닙니다. 공개 서비스로 운영하려면 인증을 붙여야 합니다.
- 공개 운영 시 `RH_LLM_DAILY_LIMIT`과 Anthropic 콘솔의 사용 한도를 함께 설정해 API 비용 상한을 두세요.
- 국립중앙도서관 API 응답 필드 매핑은 실제 인증키로 처음 호출할 때 확인하세요.
