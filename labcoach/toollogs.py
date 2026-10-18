"""분석 도구 출력 발췌: 화면에 안 보이는 레지스트리·파일·프로세스·네트워크 흐름을 맥락으로 함께 보낸다.

지원 형식(자동 판별)
- Process Monitor CSV 내보내기: 관심 있는 동작(레지스트리 쓰기, 파일 쓰기, 프로세스 생성, 네트워크)만 고른다.
- Regshot 비교 결과(텍스트): 추가·변경·삭제된 키·값·파일 줄을 고른다.
- 그 밖의 텍스트(x64dbg 로그, Sysmon 내보내기 등): 마지막 줄들.

큰 파일은 끝부분만 읽는다. 결과는 마스킹 전 텍스트이므로 보내기 전에 반드시 privacy.mask_text를 거친다.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

TAIL_BYTES = 256 * 1024
MAX_LINES = 40
MAX_CHARS = 6000

PROCMON_INTERESTING = (
    "RegSetValue",
    "RegCreateKey",
    "RegDeleteValue",
    "RegDeleteKey",
    "WriteFile",
    "SetDispositionInformationFile",
    "SetRenameInformationFile",
    "Process Create",
    "Process Start",
    "Load Image",
    "TCP Connect",
    "TCP Send",
    "UDP Send",
)
REGSHOT_SECTIONS = (
    "keys added",
    "keys deleted",
    "values added",
    "values deleted",
    "values modified",
    "files added",
    "files deleted",
    "files [attributes?] modified",
    "folders added",
    "folders deleted",
)


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    # 끝부분만 읽으면 UTF-16 BOM이 잘려 나갈 수 있다: 홀수 위치 NUL이 많으면 UTF-16LE로 본다
    if raw[1::2].count(0) > len(raw) // 4:
        return raw[len(raw) % 2 :].decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_tail(path: Path, limit: int = TAIL_BYTES) -> tuple[str, bool]:
    """(텍스트, 잘렸는지). 앞부분이 잘렸으면 첫 줄(일부만 남은 줄)은 버린다."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(4)
        if size > limit:
            fh.seek(size - limit)
            raw = fh.read()
            truncated = True
        else:
            fh.seek(0)
            raw = fh.read()
            truncated = False
    if truncated and head.startswith((b"\xff\xfe", b"\xfe\xff")):
        raw = head[:2] + raw[len(raw) % 2 :]
    text = _decode(raw)
    if truncated:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text, truncated


def summarize_procmon(text: str, header: list[str] | None = None) -> list[str]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    if header is None and rows and "Operation" in rows[0]:
        header, rows = rows[0], rows[1:]
    if not header:
        return []
    col = {name: i for i, name in enumerate(header)}
    needed = ("Process Name", "Operation", "Path")
    if not all(n in col for n in needed):
        return []
    out: list[str] = []
    seen: set[tuple] = set()
    for row in rows:
        if len(row) < len(header):
            continue
        op = row[col["Operation"]]
        if not op.startswith(PROCMON_INTERESTING):
            continue
        proc, path = row[col["Process Name"]], row[col["Path"]]
        key = (proc, op, path)
        if key in seen:
            continue  # 같은 동작 반복은 한 번만
        seen.add(key)
        result = row[col["Result"]] if "Result" in col else ""
        detail = row[col["Detail"]] if "Detail" in col else ""
        pid = row[col["PID"]] if "PID" in col else ""
        line = f"{proc}({pid}) {op} {path}"
        if result and result != "SUCCESS":
            line += f" [{result}]"
        if detail and op.startswith(("RegSetValue", "Process Create", "TCP", "UDP")):
            line += f" | {detail[:160]}"
        out.append(line)
    return out[-MAX_LINES:]


def summarize_regshot(text: str) -> list[str]:
    out: list[str] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or set(line) <= {"-", "="}:
            continue
        lower = line.lower().rstrip(":")
        head = lower.split(":")[0].strip()
        if any(head.startswith(s) for s in REGSHOT_SECTIONS):
            section = line.split(":")[0].strip()
            continue
        if lower.startswith("total changes"):
            out.append(line)
            section = ""
            continue
        if section:
            out.append(f"[{section}] {line}")
    return out[-MAX_LINES:]


def detect_kind(text: str) -> str:
    first = text.lstrip()[:400]
    if "Operation" in first and "Process Name" in first:
        return "procmon"
    if "regshot" in text[:2000].lower() or any(f"{s}:" in text.lower() for s in ("keys added", "values added", "values modified")):
        return "regshot"
    return "text"


def excerpt(path: Path, procmon_header: list[str] | None = None) -> tuple[str, str]:
    """(종류, 발췌 텍스트). 파일이 없으면 ('missing', '')."""
    path = Path(path)
    if not path.is_file():
        return "missing", ""
    text, truncated = read_tail(path)
    kind = detect_kind(text)
    if kind == "text" and truncated and procmon_header is None:
        # 큰 Procmon CSV는 끝부분에 헤더가 없다: 파일 앞부분에서 헤더를 읽어 온다
        with path.open("rb") as fh:
            head = _decode(fh.read(4096)).splitlines()
        if head and "Operation" in head[0] and "Process Name" in head[0]:
            procmon_header = next(csv.reader([head[0]]))
            kind = "procmon"
    if kind == "procmon":
        lines = summarize_procmon(text, procmon_header)
    elif kind == "regshot":
        lines = summarize_regshot(text)
    else:
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()][-MAX_LINES:]
    body = "\n".join(lines)
    if len(body) > MAX_CHARS:
        body = body[-MAX_CHARS:]
    return kind, body
