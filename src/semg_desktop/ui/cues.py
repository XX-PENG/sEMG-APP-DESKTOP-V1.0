"""场边提示音：每个音都对应一个看得见的界面事件，不做背景噪声。

watch 单声轻点、warn 双声上行、stop 三声急促——严重度靠音型区分而不是靠音量吓人。
顶栏一个开关静音全部；默认开，因为这是监护台而不是阅读页。
"""

from __future__ import annotations

import math
import struct
import tempfile
import wave
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QSoundEffect

_RATE = 22050


def _tone(freq: float, ms: float, gain: float = 0.5, shape: str = "sine") -> list[float]:
    n = int(_RATE * ms / 1000.0)
    out = []
    for i in range(n):
        t = i / _RATE
        env = min(1.0, i / (_RATE * 0.008)) * min(1.0, (n - i) / (_RATE * 0.03))
        if shape == "sine":
            v = math.sin(2 * math.pi * freq * t)
        else:
            v = math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(4 * math.pi * freq * t)
        out.append(v * env * gain)
    return out


def _silence(ms: float) -> list[float]:
    return [0.0] * int(_RATE * ms / 1000.0)


def _write_wav(path: Path, samples: list[float]) -> None:
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(_RATE)
        fh.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))


class CueAudio:
    """三种提示音 + 两个操作音，惰性生成 WAV，设备缺失时静默降级。"""

    def __init__(self) -> None:
        self.enabled = True
        self.available = False
        self._effects: dict[str, QSoundEffect] = {}
        self._dir = Path(tempfile.gettempdir()) / "semg_guard_cues"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            patterns = {
                "watch": _tone(880, 90, 0.32),
                "warn": _tone(660, 110, 0.42) + _silence(50) + _tone(880, 130, 0.42),
                "stop": (_tone(1046, 90, 0.5, "bright") + _silence(55)) * 3,
                "start": _tone(523, 90, 0.3) + _silence(40) + _tone(784, 140, 0.3),
                "stoprec": _tone(784, 90, 0.3) + _silence(40) + _tone(523, 150, 0.3),
                "ack": _tone(1318, 60, 0.22),
            }
            for name, samples in patterns.items():
                path = self._dir / f"{name}.wav"
                if not path.is_file():
                    _write_wav(path, samples)
                eff = QSoundEffect()
                eff.setSource(QUrl.fromLocalFile(str(path)))
                eff.setLoopCount(1)
                eff.setVolume(0.85)
                self._effects[name] = eff
            self.available = True
        except Exception:
            self.available = False

    def play(self, name: str) -> None:
        if not self.enabled or not self.available:
            return
        eff = self._effects.get(name)
        if eff is None:
            return
        try:
            if eff.isPlaying():
                eff.stop()
            eff.play()
        except Exception:
            pass

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not on:
            for eff in self._effects.values():
                try:
                    eff.stop()
                except Exception:
                    pass

    def shutdown(self) -> None:
        for eff in self._effects.values():
            try:
                eff.stop()
                eff.deleteLater()
            except Exception:
                pass
        self._effects.clear()