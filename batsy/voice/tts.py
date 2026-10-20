"""Speech output: Piper neural TTS (local, natural) with espeak-ng as a fallback.

Sentences are queued and spoken in order; stop() cuts speech off immediately (barge-in).
"""
import logging
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from batsy.config import DATA_DIR, MODELS_DIR

log = logging.getLogger("batsy.tts")

PLAYERS = [["pw-play"], ["paplay"], ["aplay", "-q"], ["afplay"]]


def _player() -> list[str] | None:
    return next((p for p in PLAYERS if shutil.which(p[0])), None)


def _write_chime(path: Path) -> None:
    rate = 22050
    tones = []
    for freq, dur in ((880, 0.07), (1320, 0.09)):
        t = np.linspace(0, dur, int(rate * dur), endpoint=False)
        env = np.minimum(1, np.minimum(t, dur - t) * 60)
        tones.append(0.25 * np.sin(2 * np.pi * freq * t) * env)
    pcm = (np.concatenate(tones) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


class Speaker:
    def __init__(self, cfg, enabled: bool = True):
        self.cfg = cfg
        self.enabled = enabled
        self.player = _player()
        self.q: queue.Queue = queue.Queue()
        self.generation = 0
        self.proc: subprocess.Popen | None = None
        self.piper = None
        self.engine = "none"
        self.chime_path = DATA_DIR / "chime.wav"
        if enabled:
            self._init_engine()
            if not self.chime_path.exists():
                _write_chime(self.chime_path)
            threading.Thread(target=self._worker, daemon=True, name="tts").start()

    def _init_engine(self) -> None:
        want = self.cfg.get("voice.tts_engine")
        if want in ("auto", "piper"):
            voice = MODELS_DIR / "piper" / f"{self.cfg.get('voice.piper_voice')}.onnx"
            try:
                from piper import PiperVoice

                if voice.exists() and self.player:
                    self.piper = PiperVoice.load(str(voice))
                    self.engine = "piper"
                    return
                log.warning("piper voice %s or audio player missing", voice)
            except Exception as e:
                log.warning("piper unavailable: %s", e)
        if shutil.which("espeak-ng"):
            self.engine = "espeak-ng"
        elif shutil.which("say"):  # macOS, handy for development
            self.engine = "say"
        log.info("TTS engine: %s", self.engine)

    # --- public API -------------------------------------------------------
    def say(self, text: str) -> None:
        log.info("say: %s", text)
        if self.enabled:
            self.q.put((self.generation, text))

    def chime(self) -> None:
        if self.enabled and self.cfg.get("voice.chime") and self.player:
            subprocess.Popen([*self.player, str(self.chime_path)], stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        self.generation += 1
        try:
            while True:
                self.q.get_nowait()
                self.q.task_done()
        except queue.Empty:
            pass
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()

    def idle(self) -> bool:
        return self.q.unfinished_tasks == 0

    def wait_idle(self, timeout: float = 60) -> None:
        end = time.time() + timeout
        while not self.idle() and time.time() < end:
            time.sleep(0.05)

    # --- worker -----------------------------------------------------------
    def _worker(self) -> None:
        while True:
            gen, text = self.q.get()
            try:
                if gen == self.generation:
                    self._speak(gen, text)
            except Exception as e:
                log.error("tts failed: %s", e)
            finally:
                self.q.task_done()

    def _run(self, argv: list[str], gen: int) -> None:
        if gen != self.generation:
            return
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.proc.wait()

    def _speak(self, gen: int, text: str) -> None:
        if self.engine == "piper":
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                path = f.name
            with wave.open(path, "wb") as wf:
                if hasattr(self.piper, "synthesize_wav"):  # piper-tts >= 1.3
                    self.piper.synthesize_wav(text, wf)
                else:
                    self.piper.synthesize(text, wf)
            self._run([*self.player, path], gen)
            Path(path).unlink(missing_ok=True)
        elif self.engine == "espeak-ng":
            self._run(["espeak-ng", "-s", "165", text], gen)
        elif self.engine == "say":
            self._run(["say", text], gen)
        else:
            print(f"Batsy: {text}")
