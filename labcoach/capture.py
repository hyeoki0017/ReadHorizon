"""VM 창 찾기, 화면 캡처, 보내기 전 가공(마스크 영역 가리기·축소), 화면 변화 감지.

좌표는 모두 물리 픽셀 기준이다(mss·창 위치). 오버레이(Qt)는 화면 배율로 나눠 논리 좌표로 그린다.
선택 의존성: mss(캡처), pywinctl(창 찾기). 없으면 해당 기능만 쓸 수 없다고 알려 준다.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat

from .privacy import blackout

OWN_TITLES = ("LabCoach",)
# VM 화면이 아닌 관리 창(가상머신 목록 등)은 제외한다
MANAGER_WORDS = ("manager", "관리자")


@dataclass
class Target:
    title: str
    region: tuple[int, int, int, int]  # x, y, w, h (물리 픽셀)


def find_window(hints: tuple[str, ...]) -> Target | None:
    """제목에 hints 중 하나가 들어간 보이는 창 중 가장 큰 것. pywinctl이 없으면 None."""
    try:
        import pywinctl
    except ImportError:
        return None
    best: Target | None = None
    lowered = [h.lower() for h in hints]
    for win in pywinctl.getAllWindows():
        try:
            title = win.title or ""
            if not title or any(own in title for own in OWN_TITLES):
                continue
            if not any(h in title.lower() for h in lowered):
                continue
            if any(word in title.lower() for word in MANAGER_WORDS):
                continue
            if getattr(win, "isMinimized", False) or not getattr(win, "isVisible", True):
                continue
            box = win.box  # left, top, width, height
            region = (int(box.left), int(box.top), int(box.width), int(box.height))
        except Exception:  # noqa: S112 — 창이 그 사이 닫히는 등 개별 창 오류는 건너뛴다
            continue
        if region[2] < 200 or region[3] < 150:
            continue
        if best is None or region[2] * region[3] > best.region[2] * best.region[3]:
            best = Target(title, region)
    return best


def grab(region: tuple[int, int, int, int]) -> Image.Image:
    import mss

    x, y, w, h = region
    with mss.mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def prepare(image: Image.Image, mask_regions: list[tuple[int, int, int, int]], max_edge: int) -> tuple[bytes, tuple[int, int]]:
    """마스크 영역을 칠하고 긴 변을 max_edge 이하로 줄여 PNG로. 메타데이터는 넣지 않는다."""
    image = blackout(image.convert("RGB"), mask_regions)
    longest = max(image.size)
    if longest > max_edge:
        ratio = max_edge / longest
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), image.size


class ChangeDetector:
    """작은 흑백 썸네일의 평균 밝기 차로 화면이 바뀌었는지 판단한다(같은 화면 반복 분석 = 비용 낭비)."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self._last: Image.Image | None = None

    @staticmethod
    def _thumb(image: Image.Image) -> Image.Image:
        return image.convert("L").resize((96, 54), Image.BILINEAR)

    def difference(self, image: Image.Image) -> float:
        if self._last is None:
            return 255.0
        diff = ImageChops.difference(self._thumb(image), self._last)
        return ImageStat.Stat(diff).mean[0]

    def changed(self, image: Image.Image) -> bool:
        return self.difference(image) >= self.threshold

    def accept(self, image: Image.Image) -> None:
        """분석에 보낸 화면을 기준으로 삼는다."""
        self._last = self._thumb(image)

    def reset(self) -> None:
        self._last = None


def exclude_from_capture(win_id: int) -> bool:
    """Windows 10 2004+: 이 창이 화면 캡처에 찍히지 않게 한다(오버레이가 다시 Claude에게 보이는 것 방지)."""
    if sys.platform != "win32":
        return False
    import ctypes

    WDA_EXCLUDEFROMCAPTURE = 0x11
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(win_id), WDA_EXCLUDEFROMCAPTURE))
    except (AttributeError, OSError):
        return False
