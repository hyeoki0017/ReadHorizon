"""LabCoach 오버레이·패널을 화면 없이(offscreen) 띄워 분석 → 표시 → 노트 저장 흐름을 확인한다. PySide6가 없으면 건너뛴다."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PIL import Image  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import labcoach.overlay as overlay  # noqa: E402
from labcoach.coach import DemoCoach, LLMError  # noqa: E402
from labcoach.config import Settings  # noqa: E402
from labcoach.session import Session  # noqa: E402
from labcoach.tts import Speaker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _wait(app, predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _controller(app, tmp_path, monkeypatch, coach):
    monkeypatch.setattr(overlay, "grab", lambda region: Image.new("RGB", (region[2], region[3]), (40, 40, 40)))
    settings = Settings(llm_mode="demo", data_dir=tmp_path, tts=False, hotkeys=False)
    session = Session(settings, coach)
    speaker = Speaker(enabled=False)
    controller = overlay.Controller(session, speaker, tmp_path / "notes.md", fixed_region=(0, 0, 800, 500))
    controller.start()
    return controller


def test_analyze_shows_step_and_saves_notes(app, tmp_path, monkeypatch):
    c = _controller(app, tmp_path, monkeypatch, DemoCoach())
    assert c.panel.target_label.text().startswith("대상: 지정 영역")
    assert c.overlay.width() > 0

    c.analyze_now("Run 키가 뭐야? <b>태그</b>")
    assert _wait(app, lambda: c.overlay.step is not None)
    assert not c.busy
    html = c.panel.view.toHtml()
    assert "T1547.001" in html and "데모 모드" in html

    c.save_notes()
    notes = (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert "Run 키가 뭐야?" in notes and "지속성 확보" in notes

    # 같은 화면이면 자동 분석은 다시 보내지 않는다
    calls = []
    monkeypatch.setattr(c, "_submit", lambda image, q: calls.append(q))
    c._tick()
    assert calls == []
    c.quit()


def test_missing_vm_window_never_falls_back_to_full_screen(app, tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "find_window", lambda hints: None)
    grabbed = []
    monkeypatch.setattr(overlay, "grab", lambda region: grabbed.append(region))
    settings = Settings(llm_mode="demo", data_dir=tmp_path, tts=False, hotkeys=False)
    c = overlay.Controller(Session(settings, DemoCoach()), Speaker(enabled=False), None)
    c.start()
    c.analyze_now()
    c.set_overlay_visible(True)
    assert c.target is None and grabbed == []
    assert not c.overlay.isVisible()
    assert "VM 창을 찾지 못했습니다" in c.panel.target_label.text()
    c.quit()


def test_error_is_shown_in_panel(app, tmp_path, monkeypatch):
    class Failing:
        mode = "live"

        def explain(self, image_png, task):
            raise LLMError("요청이 많아 잠시 제한되었습니다.")

    c = _controller(app, tmp_path, monkeypatch, Failing())
    c.analyze_now()
    assert _wait(app, lambda: not c.busy)
    assert "제한" in c.panel.status_label.text()
    assert c.overlay.step is None
    c.quit()


def test_paint_step_draws_pixels(app):
    image = QImage(640, 400, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    overlay.paint_step(painter, 640, 400, DemoCoach().explain(b"", ""), "분석 중…")
    painter.end()
    assert image.pixelColor(320, 395).alpha() > 0  # 자막 띠
    assert image.pixelColor(5, 200).alpha() == 0  # 주석 없는 곳은 투명(클릭·화면 가림 없음)
