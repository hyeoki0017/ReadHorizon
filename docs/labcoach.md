# LabCoach — 보안 실습 실시간 학습 오버레이

VirtualBox(악성코드 분석)·VMware(사이버보안 실습) 창 위에 **자막·화살표·텍스트 박스·강조 표시·음성**으로
지금 화면에서 일어나는 일을 설명합니다. 공격·방어 단계, 진행 방법, 스택·레지스터, 레지스트리 변경,
프로세스 → 파일·레지스트리 → 네트워크로 이어지는 흐름을 화면과 조작 패널에서 바로 볼 수 있습니다.

```
┌──────────── VM 창 (VirtualBox / VMware) ───────────────┐
│ [지속성 확보] → ◆ explorer.exe → ◆ sample.exe → ▣ Run키 │  ← 단계 배지 + 흐름 칩
│                                                         │
│   ┌──────────────────────────────┐ ← box(공격=빨강)     │
│   │ RegSetValue  HKCU\...\Run    │                      │
│   └──────────────────────────────┘                      │
│                 ↖ arrow  [Run 키 = 자동 실행]           │
│  [Sysmon EID 13으로 탐지] ← label(방어=초록)            │
│▓▓▓▓▓ sample.exe가 Run 키에 경로를 써서 재부팅 후 실행 ▓▓▓│  ← 자막 (+ 음성)
└─────────────────────────────────────────────────────────┘
  조작 패널: 지금 분석 · 자동 분석 · 모드/수준 · 질문 · 흐름/스택/레지스트리/MITRE/방어 팁/안전 주의 · 학습 노트 저장
```

## 동작 방식

1. **호스트 PC**에서 VM 창(제목에 `VirtualBox`·`VMware`가 들어간 창)을 찾아 그 영역만 캡처합니다.
2. 지정한 마스크 영역을 검게 칠하고 축소한 뒤, 창 제목·질문·도구 로그를 마스킹해 Claude에 보냅니다.
3. Claude가 구조화된 JSON(단계, 자막, 음성 문장, 0~1000 정규화 좌표의 주석, 흐름, 스택, 레지스트리, MITRE ATT&CK, 다음 단계, 방어 팁, 안전 주의)으로 답합니다.
4. 투명·클릭 통과 오버레이가 VM 창 위에 그리고, 패널에 상세 내용을 보여 주고, 내장 음성으로 읽습니다.
5. 자동 분석은 화면이 실제로 바뀌었을 때만 다시 보냅니다(같은 화면 반복 전송 없음 → 비용·세션 한도 절약).

## 빠른 시작 (호스트 PC — Windows 권장)

```bash
pip install -r requirements-labcoach.txt
copy .env.example .env      # ANTHROPIC_API_KEY 입력 (비우면 데모 모드)

python -m labcoach run                       # VM 창 자동 찾기, 패널에서 '지금 분석'
python -m labcoach run --auto --mode malware --log C:\lab\Logfile.CSV --notes C:\lab\노트.md
python -m labcoach run --mode attack --level intermediate --region 0,0,1920,1080

python -m labcoach once --image 캡처.png --render 결과.png   # 스크린샷 한 장 분석(GUI 없이)
python -m labcoach windows                   # 자동으로 잡히는 VM 창 확인
python -m labcoach notice                    # 최근 30일 수집 항목 고지
```

| 단축키 | 동작 |
|---|---|
| Ctrl+Alt+A | 지금 화면 분석 |
| Ctrl+Alt+H | 오버레이 보이기/숨기기 |
| Ctrl+Alt+V | 음성 켜기/끄기 |
| Ctrl+Alt+P | 자동 분석 켜기/끄기 |

### 모드

| 모드 | 대상 실습 | 설명 초점 |
|---|---|---|
| `malware` | VirtualBox 격리 VM에서 샘플 분석 | 정적(PE·문자열·임포트·패킹) → 동적(Procmon·Regshot·Process Explorer·x64dbg·Wireshark/FakeNet), 지속성·C2 증거 위치, IOC·탐지 규칙 |
| `attack` | VMware 공격 VM → 취약 실습 VM | 실행한 도구·출력의 원리, 공격 체인 단계, 대상에 남는 흔적과 방어자 관점 |
| `defense` | 이벤트 뷰어·Sysmon·방화벽·SIEM | 로그·이벤트 ID 의미, 어떤 공격의 흔적인지, 다음 조사·차단·강화 조치 |
| `general` | 그 밖의 실습 | 공격·방어 양쪽 해석 |

### 도구 로그 함께 보기 (`--log`, 여러 번 지정 가능)

화면에 다 안 보이는 레지스트리·파일·네트워크 흐름을 위해 분석 도구 출력 파일을 지정하면, 매 분석 때 끝부분을 읽어 함께 보냅니다.

- **Process Monitor**: File → Save → CSV. 레지스트리 쓰기·파일 쓰기·프로세스 생성·이미지 로드·TCP/UDP만 고르고 반복은 한 번만 보냅니다.
- **Regshot**: 비교 결과 텍스트(UTF-16 포함). 추가·변경·삭제된 키·값·파일 줄만 보냅니다.
- 그 밖의 텍스트(x64dbg 로그, Sysmon 내보내기 등): 마지막 40줄.

## 실습 환경 안전 수칙 (꼭 지키세요)

LabCoach는 **호스트 PC에서만** 실행합니다. 악성코드 VM 안에 설치하지 마세요(API 키가 샘플에 노출될 수 있습니다).

| 항목 | VirtualBox | VMware Workstation |
|---|---|---|
| 네트워크 | 호스트 전용 어댑터 또는 내부 네트워크 (NAT·브리지 금지) | Host-only (VMnet1) 또는 LAN Segment |
| 스냅샷 | 샘플 실행 전 깨끗한 스냅샷 → 분석 후 복원 | 동일 |
| 공유 기능 | 공유 폴더·공유 클립보드·드래그 앤 드롭 끄기 | Shared Folders·Drag and Drop·Copy and Paste 끄기 |
| 게스트 도구 | 분석 VM은 필요 최소한으로 | 동일 |
| 실습 대상 | 내가 소유하거나 허가받은 VM만 | 동일 |

Claude도 화면에서 브리지/NAT 네트워크, 켜진 공유 기능, 실습 대역 밖 대상을 보면 **안전 주의**로 알립니다.
튜터는 화면 해석과 방어 관점 설명에 집중하며, 새 악성코드·익스플로잇 코드나 탐지 회피 방법은 만들어 주지 않습니다.
화면·로그 속 문장은 분석 대상 데이터로만 다루고, 그 안의 지시문을 따르지 않습니다(프롬프트 인젝션 방지).

## 민감정보·수집 항목

- **보내는 것**: VM 창 영역 캡처(축소·마스크 영역 칠함), 창 제목, 도구 로그 발췌, 질문, 직전 자막 몇 줄 — 분석을 누르거나 자동 분석이 화면 변화를 감지했을 때만.
- **자동 마스킹(텍스트)**: 주민·외국인등록번호, 카드·전화번호, 이메일, 공인 IP, MAC 주소, Windows/리눅스 사용자 폴더 이름, API 키·토큰, `password=` 류 값. 실습 대역 사설 IP(10.x, 172.16–31.x, 192.168.x)는 학습에 필요해 그대로 둡니다.
- **이미지 안 글자**는 로컬에서 판독하지 않으므로, 호스트 알림·계정명 등이 보이는 곳은 `LABCOACH_MASK_REGIONS`로 가리세요.
- **저장하지 않는 것**: 화면 이미지(직접 `LABCOACH_SAVE_FRAMES=1`로 켠 경우 제외), 마스킹 전 원래 값.
- **수집 항목 기록**: `data/labcoach/collection.jsonl`에 항목 이름·시각·마스킹 개수만 남기고 30일 뒤 지웁니다. 30일마다 실행할 때 항목 고지를 자동으로 보여 줍니다(`python -m labcoach notice`로 언제든 확인).
- **음성**은 운영체제 내장 TTS로 로컬에서 합성합니다(외부 전송 없음). Windows는 설정 → 시간 및 언어 → 음성에서 한국어 음성(예: Heami)을 추가하세요.

## 비용·속도

- 기본 모델 `claude-opus-5-5`, effort `low`(실시간 응답 우선). 설명이 얕으면 `LABCOACH_EFFORT=medium`.
- 고정 지시문은 prompt caching, 화면은 긴 변 1568px로 축소, 같은 화면은 다시 보내지 않음.
- 거절(안전 정책) 시 서버 측 대체 모델(`fallbacks: "default"`)로 한 번 더 시도하고, 그래도 거절이면 패널에 알립니다.
- 요청 한도에 걸리면 `LABCOACH_INTERVAL_SEC`를 늘리세요.

## 알려진 제약

- 주석 위치는 Claude가 이미지에서 추정한 좌표라 몇 픽셀 어긋날 수 있습니다(확신도가 낮으면 패널에 표시).
- Windows 10 2004 이상에서는 오버레이·패널이 캡처에서 제외됩니다. 그 밖의 OS에서는 캡처 순간 오버레이를 잠깐 숨깁니다.
- macOS는 화면 기록·손쉬운 사용 권한이, Linux Wayland는 전역 단축키·창 찾기 제약이 있을 수 있습니다(그때는 `--region` 사용).
- 데모 모드(키 없음)는 고정 예시만 보여 주며 화면을 전송하지 않습니다.

## 구조

```
labcoach/
  cli.py        명령줄(run · once · windows · notice · purge)
  config.py     LABCOACH_* 설정
  capture.py    VM 창 찾기(pywinctl) · 캡처(mss) · 마스크·축소 · 화면 변화 감지 · 캡처 제외(Windows)
  privacy.py    텍스트 마스킹 · 이미지 영역 가리기 · 수집 항목 기록·30일 고지
  toollogs.py   Procmon CSV · Regshot · 텍스트 로그 발췌
  prompts.py    튜터 지시문(설명·좌표·안전 원칙) · 모드/수준별 안내
  schema.py     구조화 출력 JSON 스키마 · 파싱·좌표 보정
  coach.py      Claude 호출(스트리밍·adaptive thinking·대체 모델) · 데모 응답
  session.py    캡처 → 요청 구성 → 기록 → 학습 노트(Markdown)
  render.py     주석을 이미지에 그리기(once --render)
  overlay.py    투명 오버레이 · 조작 패널 · 단축키(PySide6, pynput)
  tts.py        로컬 음성(pyttsx3)
```
