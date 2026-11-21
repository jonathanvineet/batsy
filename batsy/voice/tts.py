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


def _envelope(path: str, step: float = 0.04) -> list[float]:
    """How loud the sentence is every 40 ms, 0..1 (scaled to its own loudest part): what the wave moves with."""
    import numpy as np

    with wave.open(path, "rb") as w:
        rate, data = w.getframerate(), w.readframes(w.getnframes())
    a = np.frombuffer(data, dtype="<i2").astype(np.float32)
    n = max(1, int(rate * step))
    blocks = a[: len(a) // n * n].reshape(-1, n)
    rms = np.sqrt((blocks ** 2).mean(axis=1)) if len(blocks) else np.zeros(1)
    top = max(float(np.percentile(rms, 95)), 1.0)
    return [round(min(1.0, float(x) / top), 3) for x in rms]


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
        self._lock = threading.Lock()  # stop() and starting the next sentence never interleave
        self.hushed_until = 0.0  # the kill switch: nothing is spoken until then (or until you ask something new)
        self.on_idle = None  # called when the last queued sentence has finished playing (or speech was stopped)
        self.on_play = None  # called with (loudness every 40 ms, 0.04) as each sentence starts: the cockpit's wave
        if enabled:
            self._init_engine()
            if not self.chime_path.exists():
                _write_chime(self.chime_path)
            self.pq: queue.Queue = queue.Queue()  # synthesized audio waiting to play
            threading.Thread(target=self._worker, daemon=True, name="tts").start()
            threading.Thread(target=self._player_loop, daemon=True, name="tts-play").start()

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
        if time.time() < self.hushed_until:  # killed: whatever is still finishing up stays silent
            log.info("hushed: %s", text)
            return
        log.info("say: %s", text)
        if self.enabled:
            self.q.put((self.generation, text))

    def hush(self, seconds: float) -> None:
        """The kill switch: silent now, and anything said for the next few seconds is dropped."""
        self.hushed_until = time.time() + seconds
        self.stop()

    def unhush(self) -> None:
        """You asked for something new: Batsy may speak again."""
        self.hushed_until = 0.0

    def chime(self) -> None:
        if self.enabled and self.cfg.get("voice.chime") and self.player:
            subprocess.Popen([*self.player, str(self.chime_path)], stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        with self._lock:  # silence first: the sentence playing now dies before anything else is tidied up
            self.generation += 1
            proc = self.proc
            if proc and proc.poll() is None:
                proc.kill()
        try:
            while True:
                self.q.get_nowait()
                self.q.task_done()
        except queue.Empty:
            pass
        try:  # audio already synthesized for the old answer
            while True:
                _, path = self.pq.get_nowait()
                self._discard(path)
        except (queue.Empty, AttributeError):
            pass
        self._maybe_idle()

    def _maybe_idle(self) -> None:
        if self.q.unfinished_tasks == 0 and self.on_idle:
            try:
                self.on_idle()
            except Exception:
                log.exception("on_idle")

    def idle(self) -> bool:
        return self.q.unfinished_tasks == 0

    def wait_idle(self, timeout: float = 60) -> None:
        end = time.time() + timeout
        while not self.idle() and time.time() < end:
            time.sleep(0.05)

    # --- workers: one synthesizes the next sentence while the other plays the current one -----------------
    def _worker(self) -> None:
        while True:
            gen, text = self.q.get()
            if gen != self.generation:
                self.q.task_done()
                continue
            try:
                path = self._synth(text) if self.engine == "piper" else None
            except Exception as e:
                log.error("tts failed: %s", e)
                self.q.task_done()
                continue
            if path is None:  # engines that synthesize and play in one go
                try:
                    if gen == self.generation:
                        self._speak(gen, text)
                except Exception as e:
                    log.error("tts failed: %s", e)
                finally:
                    self.q.task_done()
                    self._maybe_idle()
            else:
                self.pq.put((gen, path))

    def _player_loop(self) -> None:
        while True:
            gen, path = self.pq.get()
            try:
                if self.on_play is not None and gen == self.generation:
                    try:
                        self.on_play(_envelope(path), 0.04)
                    except Exception:
                        pass
                self._run([*self.player, path], gen)
            except Exception as e:
                log.error("audio playback failed: %s", e)
            finally:
                Path(path).unlink(missing_ok=True)
                self.q.task_done()
                self._maybe_idle()

    def _discard(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)
        self.q.task_done()

    def _run(self, argv: list[str], gen: int) -> None:
        with self._lock:  # a stop() between the check and the start would miss this sentence
            if gen != self.generation:
                return
            proc = self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()

    def _synth(self, text: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        with wave.open(path, "wb") as wf:
            if hasattr(self.piper, "synthesize_wav"):  # piper-tts >= 1.3
                self.piper.synthesize_wav(text, wf)
            else:
                self.piper.synthesize(text, wf)
        return path

    def _speak(self, gen: int, text: str) -> None:
        if self.engine == "espeak-ng":
            self._run(["espeak-ng", "-s", "165", text], gen)
        elif self.engine == "say":
            self._run(["say", text], gen)
        else:
            print(f"Batsy: {text}")
