# exam_booker — 자격증 시험 1인 접수 도우미 (Q-Net · KCA · KISA)

학원 일괄 접수 등으로 시험장이 빠르게 마감될 때, **암호화해 둔 내 계정으로 접수 직전 자동 로그인하고,
정해 둔 시각에 접수 화면을 열어 희망 우선순위대로 날짜·시간·시험장을 골라 주는** 프로그램입니다.
본인 PC에서 본인 계정 1개로만 쓰는 용도입니다. (ReadHorizon 본체와는 별개 도구)

보안·개인정보 점검 결과는 [SECURITY.md](SECURITY.md)를 보세요.

## 동작 순서

1. 마스터 비밀번호로 암호화 보관함을 엽니다.
   접수 `login.lead_minutes`분 전(기본 5분)에 보관함의 아이디·비밀번호로 **자동 로그인**합니다(1회만 시도).
   자동 로그인 설정이 없거나 실패하면 브라우저에서 직접 로그인 → 터미널에서 Enter
2. 접수 사이트의 서버 시각으로 시계를 보정하고, 시작 `lead_seconds`초 전까지 대기
3. 접수 버튼이 활성화될 때까지 `poll_interval`(최소 1초) 간격으로 새로고침
4. 일정 목록에서 **1순위 → 2순위 → …** 순서로, 자리가 남은 첫 일정을 선택
   - `마감`·`매진`·`잔여 0`·`0석`·`0/30` 표시는 자리 없음으로 판단
   - 희망 조건이 모두 마감이면 취소 자리를 기다리며 계속 확인(`give_up_after_minutes`까지)
5. 최종 접수 버튼을 빨간 테두리로 표시하고 알림음 → **최종 확인·결제는 직접**
   (`auto_confirm: true`면 최종 접수 버튼까지 자동으로 누름. 결제 정보 입력은 항상 사람이 합니다)

## 설치·실행

```bash
cd exam_booker
pip install -r requirements.txt
python -m playwright install chromium
python -m exam_booker vault add qnet          # 아이디·비밀번호를 암호화 저장 (kca, kisa도 같은 방식)
python -m exam_booker vault list              # 저장 목록 (아이디 가림 표시)
cp config.example.yaml config.yaml            # site·주소·시각·희망 조건·선택자 수정
python -m exam_booker run config.yaml --check # 설정 검사
python -m exam_booker run config.yaml         # 실행 (자동 로그인 → 시작 시각까지 대기 → 접수)
python -m exam_booker cleanup --vault-too     # 접수 끝난 뒤 로그인 쿠키·보관함 삭제
```

### 사이트 프리셋

`site: qnet | kca | kisa`를 적으면 로그인 화면용 기본 선택자와 대기열(넷퍼넬)·보안문자 감지가 채워집니다.
(`python -m exam_booker sites`로 목록 확인) 프리셋은 **실제 사이트에서 검증되지 않은 출발점**입니다.
`python -m exam_booker inspect <로그인 주소>`로 화면의 입력칸·버튼 선택자를 확인해
`login.id_field`, `login.pw_field`, `login.submit_button`, `login.logged_in_marker`를 덮어쓰세요.
KISA는 응시할 자격의 접수 사이트 주소로 `login_url`·`apply_url`을 바꿔야 합니다.

### 연습: 모의 접수 사이트로 먼저 해 보기

`config.yaml`에서 `apply_url`을 `mock/mock_site.html`의 파일 주소로 바꾸고 `--now`로 실행합니다.

```bash
# 예: apply_url: file:///절대경로/exam_booker/mock/mock_site.html?open=0
python -m exam_booker config.yaml --now
```

## 실제 사이트에 맞추기 (선택자)

사이트마다 화면 구조가 달라서 **CSS 선택자를 설정**해야 합니다. `inspect` 명령이나 F12(개발자도구) →
요소 검사로 확인하세요. 접수가 열리기 전에 **지난 회차 화면이나 추가접수 화면에서 미리 맞춰 보길** 권합니다.

| 항목 | 의미 |
|---|---|
| `apply_button` | 원서접수 버튼 |
| `before_slots` | 일정 목록을 띄우기 전 누를 것(지역 선택·조회 버튼 등), 순서대로 |
| `slot_row` | 일정 한 줄(시험일·시간·시험장·잔여석이 보이는 행) |
| `slot_choose` | 그 행 안의 '선택' 버튼 |
| `confirm_button` | 최종 접수 버튼 |
| `blockers` | 보이면 멈출 화면(보안문자, 대기열 팝업 등) |

희망 조건의 날짜는 `2026-11-21`, `2026.11.21`, `11월 21일`을 같은 것으로 인식하고, 공백 차이는 무시합니다.

## 지키는 원칙 (꼭 읽어 주세요)

- **개인정보 최소화**: 다루는 정보는 접수 사이트 아이디·비밀번호뿐이며, 내 PC의 `credentials.enc`에
  AES-256-GCM으로 암호화해 저장합니다(마스터 비밀번호는 저장하지 않음). 설정 파일에 비밀번호를 적으면 실행을 거부하고,
  화면·로그에는 아이디를 가려서(`sa****01`) 표시합니다. 외부로 보내는 데이터는 없습니다
  (접수 사이트 접속과 서버 시각 확인용 HEAD 요청뿐). 로그인 쿠키는 `browser_profile/`에 남으니 사용 후 `cleanup` 하세요.
  `credentials.enc`·`browser_profile/`·`config.yaml`은 `.gitignore`로 커밋을 막아 두었습니다.
- **보안문자·대기열은 우회하지 않습니다.** 감지되면 알림을 띄우고 사람이 처리할 때까지 기다립니다.
- **새로고침은 1초 이상 간격만 허용**합니다. 과도한 요청은 사이트 차단·이용 제한의 원인이 됩니다.
- **이용약관 확인**: Q-Net 등 일부 접수 사이트는 매크로·자동화 프로그램 사용을 약관으로 제한하며, 적발 시
  접수 취소나 이용 제한이 있을 수 있습니다. 사용 전 해당 사이트 약관을 확인하고, **본인 1인 접수 용도로만**
  사용하세요. 여러 계정 대리 접수·자리 선점 후 양도 등에는 사용하지 마세요.
- 사이트 화면이 바뀌면 선택자가 맞지 않아 실패할 수 있으니, 접수 당일에도 브라우저 화면을 지켜보며
  필요하면 직접 진행하세요.

## 테스트

```bash
python -m pytest -q tests   # 로직 테스트 + 모의 사이트 브라우저 테스트(Chromium 있을 때)
```
