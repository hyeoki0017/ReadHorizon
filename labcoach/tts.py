"""음성 설명: 운영체제 내장 음성(pyttsx3 — Windows SAPI5, macOS NSSpeech, Linux eSpeak)으로 로컬에서 읽는다.

음성 합성을 외부로 보내지 않는다. 밀린 설명은 버리고 가장 최근 설명만 읽는다(화면과 음성이 어긋나지 않게).
"""

from __future__ import annotations

import queue
import threading


class Speaker:
    def __init__(self, voice_hint: str = "", rate: int = 180, enabled: bool = True) -> None:
        self.enabled = enabled
        self.available = False
        self.voice_hint = voice_hint.lower()
        self.rate = rate
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        try:
            import pyttsx3  # noqa: F401
        except ImportError:
            return
        self.available = True
        self._thread = threading.Thread(target=self._run, name="labcoach-tts", daemon=True)
        self._thread.start()

    def _pick_voice(self, engine) -> None:
        voices = engine.getProperty("voices") or []
        hints = [self.voice_hint] if self.voice_hint else []
        hints += ["korean", "ko_kr", "ko-kr", "heami", "yuna", "sunhi"]
        for hint in hints:
            for voice in voices:
                text = f"{getattr(voice, 'id', '')} {getattr(voice, 'name', '')} {getattr(voice, 'languages', '')}".lower()
                if hint and hint in text:
                    engine.setProperty("voice", voice.id)
                    return

    def _run(self) -> None:
        import pyttsx3

        # 엔진은 이 스레드 안에서 만들어야 한다(Windows COM 초기화)
        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        self._pick_voice(engine)
        while True:
            text = self._queue.get()
            if text is None:
                break
            while not self._queue.empty():  # 밀린 것은 버리고 최신만
                newer = self._queue.get()
                if newer is None:
                    return
                text = newer
            try:
                engine.say(text)
                engine.runAndWait()
            except RuntimeError:
                continue

    def say(self, text: str) -> None:
        if self.enabled and self.available and text:
            self._queue.put(text)

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
