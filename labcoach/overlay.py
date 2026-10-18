"""실시간 오버레이(PySide6).

- OverlayWindow: VM 창 위에 겹치는 투명·클릭 통과 창. 화살표·박스·라벨·강조, 위쪽 흐름 칩, 아래쪽 자막을 그린다.
- PanelWindow: 조작 패널. 지금 분석 / 자동 분석 / 오버레이·음성 켜고 끄기 / 모드·수준 / 질문,
  그리고 흐름·스택·레지스트리·MITRE·방어 팁·다음 단계·안전 주의 표시.
- Claude 호출은 작업 스레드 하나에서만 하고(동시에 한 건), 결과는 Qt 시그널로 GUI 스레드에 넘긴다.

캡처에 오버레이가 찍히지 않도록 Windows에서는 캡처 제외(SetWindowDisplayAffinity)를 쓰고,
그 밖의 OS에서는 캡처하는 순간만 오버레이를 숨긴다.
"""

from __future__ import annotations

import html
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .capture import ChangeDetector, Target, exclude_from_capture, find_window, grab
from .coach import LLMError
from .config import LEVELS, MODES
from .render import FLOW_ICONS, TONE_COLORS, arrow_head
from .schema import CoachStep, scale_annotation
from .session import Session
from .tts import Speaker

MODE_LABELS = {"malware": "악성코드 분석", "attack": "공격 실습", "defense": "방어 실습", "general": "일반"}
LEVEL_LABELS = {"beginner": "입문", "intermediate": "중급", "advanced": "고급"}


def _qcolor(tone: str, alpha: int = 255) -> QColor:
    r, g, b = TONE_COLORS.get(tone, TONE_COLORS["info"])
    return QColor(r, g, b, alpha)


class Bridge(QObject):
    """작업 스레드·단축키 스레드 → GUI 스레드."""

    step_ready = Signal(object, str)  # CoachStep, 질문
    failed = Signal(str)
    hotkey = Signal(str)


class OverlayWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setWindowTitle("LabCoach Overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.step: CoachStep | None = None
        self.status = ""

    def place(self, region: tuple[int, int, int, int]) -> None:
        """물리 픽셀 영역 → 논리 좌표로 창 위치 맞추기."""
        x, y, w, h = region
        screen = QGuiApplication.screenAt(QPointF(x + w / 2, y + h / 2).toPoint()) or QGuiApplication.primaryScreen()
        ratio = screen.devicePixelRatio() if screen else 1.0
        self.setGeometry(round(x / ratio), round(y / ratio), max(1, round(w / ratio)), max(1, round(h / ratio)))

    def show_step(self, step: CoachStep | None) -> None:
        self.step = step
        self.update()

    def set_status(self, text: str) -> None:
        self.status = text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt 메서드 이름
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_step(painter, self.width(), self.height(), self.step, self.status)
        painter.end()


def paint_step(painter: QPainter, W: int, H: int, step: CoachStep | None, status: str = "") -> None:
    """오버레이 그리기. 테스트에서 QImage에 그려 확인할 수 있게 함수로 뺐다."""
    unit = max(12, round(min(W, H) / 45))
    small = QFont()
    small.setPixelSize(unit)
    big = QFont()
    big.setPixelSize(round(unit * 1.25))
    big.setBold(True)

    def text_box(x: float, y: float, text: str, tone: str) -> None:
        painter.setFont(small)
        metrics = QFontMetrics(small)
        rect = metrics.boundingRect(0, 0, max(120, W // 3), H, Qt.TextFlag.TextWordWrap, text)
        pad = 6
        bw, bh = rect.width() + pad * 2, rect.height() + pad * 2
        x = max(0, min(x, W - bw))
        y = max(0, min(y, H - bh))
        box = QRectF(x, y, bw, bh)
        painter.setPen(QPen(_qcolor(tone), 2))
        painter.setBrush(QColor(15, 23, 42, 215))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(box.adjusted(pad, pad, -pad, -pad), Qt.TextFlag.TextWordWrap, text)

    if step is not None:
        stroke = max(2, unit // 5)
        for ann in step.annotations:
            x, y, x2, y2 = scale_annotation(ann, W, H)
            color = _qcolor(ann.tone)
            if ann.kind == "box":
                painter.setPen(QPen(color, stroke))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(x, y, x2 - x, y2 - y), 4, 4)
                if ann.text:
                    text_box(x, y - unit * 2.2, ann.text, ann.tone)
            elif ann.kind == "highlight":
                painter.setPen(QPen(_qcolor(ann.tone, 160), 1))
                painter.setBrush(_qcolor(ann.tone, 60))
                painter.drawRect(QRectF(x, y, x2 - x, y2 - y))
                if ann.text:
                    text_box(x, y2 + 4, ann.text, ann.tone)
            elif ann.kind == "arrow":
                painter.setPen(QPen(color, stroke + 1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(QPointF(x, y), QPointF(x2, y2))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawPolygon(QPolygonF([QPointF(px, py) for px, py in arrow_head(x, y, x2, y2, unit * 1.3)]))
                if ann.text:
                    text_box(x - unit, y, ann.text, ann.tone)
            else:
                text_box(x, y, ann.text, ann.tone)

        # 위: 단계 배지와 흐름 칩
        painter.setFont(small)
        metrics = QFontMetrics(small)
        chips = [step.phase_label] + [f"{FLOW_ICONS.get(n.kind, '•')} {n.label}" for n in step.flow]
        cx, cy = unit / 2, unit / 2
        for index, chip in enumerate(chips):
            width = metrics.horizontalAdvance(chip) + unit
            if cx + width > W - unit:
                break
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(99, 102, 241, 230) if index == 0 else QColor(30, 41, 59, 215))
            painter.drawRoundedRect(QRectF(cx, cy, width, unit * 1.8), unit * 0.9, unit * 0.9)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QRectF(cx, cy, width, unit * 1.8), Qt.AlignmentFlag.AlignCenter, chip)
            cx += width
            if index < len(chips) - 1:
                painter.setPen(QColor(226, 232, 240))
                painter.drawText(QRectF(cx, cy, unit * 1.4, unit * 1.8), Qt.AlignmentFlag.AlignCenter, "→")
                cx += unit * 1.4

        # 아래: 자막
        if step.subtitle:
            painter.setFont(big)
            metrics = QFontMetrics(big)
            area = metrics.boundingRect(0, 0, W - unit * 4, H, Qt.TextFlag.TextWordWrap, step.subtitle)
            top = H - area.height() - unit * 2
            path = QPainterPath()
            path.addRect(QRectF(0, top, W, H - top))
            painter.fillPath(path, QColor(0, 0, 0, 170))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(
                QRectF(unit * 2, top + unit, W - unit * 4, area.height()),
                Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
                step.subtitle,
            )

    if status:
        painter.setFont(small)
        metrics = QFontMetrics(small)
        width = metrics.horizontalAdvance(status) + unit
        box = QRectF(W - width - unit / 2, unit / 2, width, unit * 1.8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor(250, 204, 21))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, status)


def step_html(step: CoachStep | None) -> str:
    """패널에 보일 HTML. 모든 값은 이스케이프한다(화면 속 글자가 HTML로 해석되지 않게)."""
    if step is None:
        return "<p>아직 분석한 화면이 없습니다. <b>지금 분석</b>을 누르거나 자동 분석을 켜세요.</p>"
    e = html.escape
    parts = []
    if step.demo:
        parts.append("<p style='color:#b45309'><b>데모 모드</b>: 실제 화면 분석이 아닙니다. ANTHROPIC_API_KEY를 설정하세요.</p>")
    parts.append(f"<h3>[{e(step.phase_label)}] {e(step.subtitle)}</h3>")
    if step.screen_summary:
        parts.append(f"<p>{e(step.screen_summary)}</p>")
    if step.narration:
        parts.append(f"<p style='color:#475569'><i>{e(step.narration)}</i></p>")
    if step.safety_warnings:
        parts.append("<p style='color:#b91c1c'><b>⚠ 안전 주의</b></p><ul>" + "".join(f"<li>{e(w)}</li>" for w in step.safety_warnings) + "</ul>")
    if step.flow:
        chain = " → ".join(
            f"<b>{e(FLOW_ICONS.get(n.kind, '•'))} {e(n.label)}</b>" + (f" <small>({e(n.detail)})</small>" if n.detail else "")
            for n in step.flow
        )
        parts.append(f"<p><b>흐름</b><br>{chain}</p>")

    def table(title: str, head: tuple[str, str], rows) -> str:
        body = "".join(f"<tr><td><code>{e(r.first)}</code></td><td><code>{e(r.second)}</code></td><td>{e(r.meaning)}</td></tr>" for r in rows)
        return (
            f"<p><b>{title}</b></p><table border='1' cellspacing='0' cellpadding='3'>"
            f"<tr><th>{head[0]}</th><th>{head[1]}</th><th>의미</th></tr>{body}</table>"
        )

    if step.stack:
        parts.append(table("스택·레지스터", ("위치", "값"), step.stack))
    if step.registry:
        parts.append(table("레지스트리", ("키", "값"), step.registry))
    if step.mitre:
        parts.append("<p><b>MITRE ATT&amp;CK</b>: " + ", ".join(f"{e(i)} {e(n)}" for i, n in step.mitre) + "</p>")
    for title, values in (("다음 단계", step.next_steps), ("방어 팁", step.defense_tips)):
        if values:
            parts.append(f"<p><b>{title}</b></p><ul>" + "".join(f"<li>{e(v)}</li>" for v in values) + "</ul>")
    meta = f"확신도 {step.confidence} · 모델 {step.model or '-'}"
    if step.fallback_used:
        meta += " · 대체 모델 응답"
    parts.append(f"<p style='color:#94a3b8'><small>{e(meta)}</small></p>")
    return "".join(parts)


class PanelWindow(QWidget):
    def __init__(self, app_controller: Controller) -> None:
        super().__init__(None, Qt.WindowType.WindowStaysOnTopHint)
        self.c = app_controller
        self.setWindowTitle("LabCoach")
        self.resize(440, 640)

        self.analyze_btn = QPushButton("지금 분석 (Ctrl+Alt+A)")
        self.auto_box = QCheckBox("자동 분석")
        self.overlay_box = QCheckBox("오버레이")
        self.overlay_box.setChecked(True)
        self.voice_box = QCheckBox("음성")
        self.mode_combo = QComboBox()
        for m in MODES:
            self.mode_combo.addItem(MODE_LABELS[m], m)
        self.level_combo = QComboBox()
        for lv in LEVELS:
            self.level_combo.addItem(LEVEL_LABELS[lv], lv)
        self.question = QLineEdit()
        self.question.setPlaceholderText("질문 (예: 이 레지스트리 값은 왜 위험해?)")
        self.ask_btn = QPushButton("질문")
        self.target_label = QLabel()
        self.target_label.setTextFormat(Qt.TextFormat.PlainText)
        self.target_label.setWordWrap(True)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.save_btn = QPushButton("학습 노트 저장")

        row1 = QHBoxLayout()
        row1.addWidget(self.analyze_btn)
        row1.addWidget(self.auto_box)
        row2 = QHBoxLayout()
        row2.addWidget(self.mode_combo)
        row2.addWidget(self.level_combo)
        row2.addWidget(self.overlay_box)
        row2.addWidget(self.voice_box)
        row3 = QHBoxLayout()
        row3.addWidget(self.question)
        row3.addWidget(self.ask_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(self.target_label)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        layout.addWidget(self.status_label)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.save_btn)

        self.analyze_btn.clicked.connect(lambda: self.c.analyze_now())
        self.ask_btn.clicked.connect(self._ask)
        self.question.returnPressed.connect(self._ask)
        self.auto_box.toggled.connect(self.c.set_auto)
        self.overlay_box.toggled.connect(self.c.set_overlay_visible)
        self.voice_box.toggled.connect(self.c.set_voice)
        self.mode_combo.currentIndexChanged.connect(lambda _: self.c.set_mode(self.mode_combo.currentData()))
        self.level_combo.currentIndexChanged.connect(lambda _: self.c.set_level(self.level_combo.currentData()))
        self.save_btn.clicked.connect(self.c.save_notes)
        self.view.setHtml(step_html(None))

    def _ask(self) -> None:
        text = self.question.text().strip()
        if text:
            self.c.analyze_now(text)
            self.question.clear()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.c.quit()
        event.accept()


class Controller:
    def __init__(self, session: Session, speaker: Speaker, notes_path: Path | None, fixed_region=None) -> None:
        self.session = session
        self.settings = session.settings
        self.speaker = speaker
        self.notes_path = notes_path
        self.fixed_region = fixed_region
        self.target: Target | None = None
        self.detector = ChangeDetector(self.settings.change_threshold)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.busy = False
        self.bridge = Bridge()
        self.bridge.step_ready.connect(self._on_step)
        self.bridge.failed.connect(self._on_error)
        self.bridge.hotkey.connect(self._on_hotkey)
        self.overlay = OverlayWindow()
        self.panel = PanelWindow(self)
        self.panel.mode_combo.setCurrentIndex(MODES.index(self.settings.mode))
        self.panel.level_combo.setCurrentIndex(LEVELS.index(self.settings.level))
        self.panel.voice_box.setChecked(self.settings.tts and speaker.available)
        self.panel.voice_box.setEnabled(speaker.available)
        if not speaker.available:
            self.panel.voice_box.setToolTip("pyttsx3가 설치되지 않아 음성을 쓸 수 없습니다.")
        self.capture_excluded = False
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.locate_timer = QTimer()
        self.locate_timer.timeout.connect(self.locate)
        self.hotkeys = None

    # ---- 시작·종료
    def start(self) -> None:
        self.panel.show()
        self.locate()
        if self.target is not None:
            self.overlay.show()
        # 창이 만들어진 뒤에 캡처 제외 설정(Windows). 실패하면 캡처 때 숨기는 방식으로 동작
        excluded = [exclude_from_capture(int(w.winId())) for w in (self.overlay, self.panel)]
        self.capture_excluded = excluded[0]
        self.locate_timer.start(1500)  # VM 창을 옮기거나 크기를 바꾸면 따라간다
        if self.settings.hotkeys:
            self._start_hotkeys()

    def _start_hotkeys(self) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            return
        emit = self.bridge.hotkey.emit
        try:
            self.hotkeys = keyboard.GlobalHotKeys(
                {
                    "<ctrl>+<alt>+a": lambda: emit("analyze"),
                    "<ctrl>+<alt>+h": lambda: emit("overlay"),
                    "<ctrl>+<alt>+v": lambda: emit("voice"),
                    "<ctrl>+<alt>+p": lambda: emit("auto"),
                }
            )
            self.hotkeys.start()
        except Exception:  # 단축키를 못 쓰는 환경(권한·Wayland 등)에서도 패널은 동작해야 한다
            self.hotkeys = None

    def quit(self) -> None:
        self.timer.stop()
        self.locate_timer.stop()
        if self.hotkeys is not None:
            self.hotkeys.stop()
        self.speaker.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.overlay.close()
        QApplication.quit()

    # ---- 대상 창
    def locate(self) -> None:
        if self.fixed_region:
            target = Target("지정 영역", self.fixed_region)
        else:
            # 못 찾으면 모니터 전체로 넘어가지 않는다(VM 밖 화면을 보내지 않도록 — 최소 수집)
            target = find_window(self.settings.window_hints)
        if target is None:
            self.target = None
            self.overlay.hide()
            self.panel.target_label.setText(
                "VM 창을 찾지 못했습니다. VM을 실행해 창을 띄우거나(최소화 해제) --region x,y,폭,높이 로 지정하세요."
            )
            return
        if self.target is None or target.region != self.target.region or target.title != self.target.title:
            self.target = target
            self.overlay.place(target.region)
            if self.panel.overlay_box.isChecked():
                self.overlay.show()
            self.detector.reset()
            x, y, w, h = target.region
            self.panel.target_label.setText(f"대상: {target.title}  ({x},{y} {w}x{h})")

    # ---- 분석
    def _capture(self):
        hide = not self.capture_excluded and self.overlay.isVisible()
        if hide:
            self.overlay.hide()
            QApplication.processEvents()
        try:
            return grab(self.target.region)
        finally:
            if hide:
                self.overlay.show()

    def _tick(self) -> None:
        if self.busy or self.target is None:
            return
        image = self._capture()
        if not self.detector.changed(image):
            self.overlay.set_status("")
            return
        self._submit(image, "")

    def analyze_now(self, question: str = "") -> None:
        if self.target is None:
            self.locate()
            if self.target is None:
                return
        if self.busy:
            self.panel.status_label.setText("이전 분석이 끝나면 다시 눌러 주세요.")
            return
        self._submit(self._capture(), question)

    def _submit(self, image, question: str) -> None:
        self.busy = True
        self.detector.accept(image)
        self.overlay.set_status("분석 중…")
        self.panel.status_label.setText("분석 중…")
        title = self.target.title if self.target else ""
        request = self.session.build(image, title, question)

        def work():
            try:
                step = self.session.run(request, question)
            except LLMError as exc:
                self.bridge.failed.emit(str(exc))
            except Exception as exc:  # noqa: BLE001 — 예상 못 한 오류도 패널에 보여 준다
                self.bridge.failed.emit(f"오류: {exc.__class__.__name__}")
            else:
                self.bridge.step_ready.emit(step, question)

        self.executor.submit(work)

    def _on_step(self, step: CoachStep, question: str) -> None:
        self.busy = False
        self.overlay.set_status("")
        self.overlay.show_step(step)
        self.panel.view.setHtml(step_html(step))
        usage = step.usage or {}
        self.panel.status_label.setText(
            f"완료 · 입력 {usage.get('input_tokens') or 0} / 출력 {usage.get('output_tokens') or 0} 토큰"
            + (" · 데모" if step.demo else "")
        )
        self.speaker.say(step.narration or step.subtitle)

    def _on_error(self, message: str) -> None:
        self.busy = False
        self.overlay.set_status("")
        self.panel.status_label.setText(message)

    # ---- 패널 조작
    def set_auto(self, on: bool) -> None:
        if on:
            self.timer.start(self.settings.interval_sec * 1000)
            self._tick()
        else:
            self.timer.stop()

    def set_overlay_visible(self, on: bool) -> None:
        self.overlay.setVisible(on and self.target is not None)

    def set_voice(self, on: bool) -> None:
        self.speaker.enabled = on

    def set_mode(self, mode: str) -> None:
        self.session.mode = mode
        self.detector.reset()

    def set_level(self, level: str) -> None:
        self.session.level = level

    def save_notes(self) -> None:
        if not self.session.steps:
            self.panel.status_label.setText("저장할 단계가 없습니다.")
            return
        path = self.notes_path or (self.settings.data_dir / "notes.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.session.notes_markdown(), encoding="utf-8")
        self.panel.status_label.setText(f"학습 노트를 저장했습니다: {path}")

    def _on_hotkey(self, name: str) -> None:
        if name == "analyze":
            self.analyze_now()
        elif name == "overlay":
            self.panel.overlay_box.toggle()
        elif name == "voice" and self.panel.voice_box.isEnabled():
            self.panel.voice_box.toggle()
        elif name == "auto":
            self.panel.auto_box.toggle()


def run_gui(session: Session, speaker: Speaker, notes_path: Path | None, region=None, auto: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    controller = Controller(session, speaker, notes_path, region)
    controller.start()
    if auto:
        controller.panel.auto_box.setChecked(True)
    return app.exec()
