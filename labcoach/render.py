"""코칭 단계를 이미지 위에 그린다(PIL). 명령줄 `once --render`와 테스트에서 쓰고, 색·도형 계산은 오버레이와 공유한다."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .schema import CoachStep, scale_annotation

# tone → (R, G, B)
TONE_COLORS = {
    "info": (56, 189, 248),
    "attack": (248, 81, 73),
    "defense": (63, 185, 80),
    "warning": (250, 204, 21),
}
FLOW_ICONS = {
    "process": "◆",
    "registry": "▣",
    "file": "▤",
    "network": "⇄",
    "stack": "≡",
    "memory": "▦",
    "api_call": "◎",
    "event_log": "※",
    "other": "•",
}
FONT_CANDIDATES = (
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
)


def arrow_head(x1: float, y1: float, x2: float, y2: float, size: float) -> list[tuple[float, float]]:
    """(x2, y2)를 꼭짓점으로 하는 화살촉 삼각형 세 점."""
    angle = math.atan2(y2 - y1, x2 - x1)
    spread = math.radians(26)
    return [
        (x2, y2),
        (x2 - size * math.cos(angle - spread), y2 - size * math.sin(angle - spread)),
        (x2 - size * math.cos(angle + spread), y2 - size * math.sin(angle + spread)),
    ]


def font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=fnt) > width and current:
            lines.append(current)
            current = ch.lstrip()
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _text_box(draw, xy, text, color, fnt, max_width, bounds):
    x, y = xy
    lines = _wrap(draw, text, fnt, max_width)[:3]
    if not lines:
        return
    pad = 6
    height = sum(fnt.getbbox(ln)[3] for ln in lines) + pad * 2 + 2 * (len(lines) - 1)
    width = max(draw.textlength(ln, font=fnt) for ln in lines) + pad * 2
    W, H = bounds
    x = max(0, min(x, W - width))
    y = max(0, min(y, H - height))
    draw.rounded_rectangle([x, y, x + width, y + height], radius=6, fill=(15, 23, 42, 215), outline=color + (255,), width=2)
    ty = y + pad
    for ln in lines:
        draw.text((x + pad, ty), ln, font=fnt, fill=(255, 255, 255, 255))
        ty += fnt.getbbox(ln)[3] + 2


def render(image: Image.Image, step: CoachStep) -> Image.Image:
    base = image.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    W, H = base.size
    unit = max(12, round(min(W, H) / 45))
    small, big = font(unit), font(round(unit * 1.25))
    stroke = max(2, unit // 5)

    for ann in step.annotations:
        color = TONE_COLORS.get(ann.tone, TONE_COLORS["info"])
        x, y, x2, y2 = scale_annotation(ann, W, H)
        if ann.kind == "box":
            draw.rounded_rectangle([x, y, x2, y2], radius=4, outline=color + (255,), width=stroke)
            if ann.text:
                _text_box(draw, (x, y - unit * 2), ann.text, color, small, W // 3, (W, H))
        elif ann.kind == "highlight":
            draw.rectangle([x, y, x2, y2], fill=color + (60,), outline=color + (160,), width=1)
            if ann.text:
                _text_box(draw, (x, y2 + 4), ann.text, color, small, W // 3, (W, H))
        elif ann.kind == "arrow":
            draw.line([x, y, x2, y2], fill=color + (255,), width=stroke + 1)
            draw.polygon(arrow_head(x, y, x2, y2, unit * 1.3), fill=color + (255,))
            if ann.text:
                _text_box(draw, (x - unit, y), ann.text, color, small, W // 3, (W, H))
        else:  # label
            _text_box(draw, (x, y), ann.text, color, small, W // 3, (W, H))

    # 위쪽: 단계 배지 + 흐름 칩
    chips = [f"{step.phase_label}"] + [f"{FLOW_ICONS.get(n.kind, '•')} {n.label}" for n in step.flow]
    cx, cy = unit // 2, unit // 2
    for index, chip in enumerate(chips):
        width = draw.textlength(chip, font=small) + unit
        if cx + width > W - unit:
            break
        fill = (99, 102, 241, 230) if index == 0 else (30, 41, 59, 215)
        draw.rounded_rectangle([cx, cy, cx + width, cy + unit * 1.8], radius=unit, fill=fill)
        draw.text((cx + unit / 2, cy + unit * 0.3), chip, font=small, fill=(255, 255, 255, 255))
        cx += width
        if index < len(chips) - 1:
            draw.text((cx + 2, cy + unit * 0.3), "→", font=small, fill=(226, 232, 240, 255))
            cx += draw.textlength("→", font=small) + 6

    # 아래쪽: 자막
    if step.subtitle:
        lines = _wrap(draw, step.subtitle, big, W - unit * 4)[:2]
        line_h = big.getbbox("가")[3] + 4
        top = H - line_h * len(lines) - unit * 2
        draw.rectangle([0, top, W, H], fill=(0, 0, 0, 170))
        for i, ln in enumerate(lines):
            tw = draw.textlength(ln, font=big)
            draw.text(((W - tw) / 2, top + unit + i * line_h), ln, font=big, fill=(255, 255, 255, 255))

    return Image.alpha_composite(base, layer).convert("RGB")
