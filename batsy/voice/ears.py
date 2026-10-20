"""Microphone input: always-on wake word spotting (Vosk, offline) and utterance recording with a simple VAD."""
import collections
import json
import logging
import queue
import re

import numpy as np

from batsy.config import MODELS_DIR

log = logging.getLogger("batsy.ears")

RATE = 16000
BLOCK = 800  # 50 ms frames
FRAMES_PER_SEC = RATE // BLOCK


def rms(frame: bytes) -> float:
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class Ears:
    def __init__(self, cfg):
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        self.cfg = cfg
        model_dir = MODELS_DIR / cfg.get("voice.vosk_model")
        if not model_dir.exists():
            raise RuntimeError(f"Vosk model missing at {model_dir}. Run install.sh.")
        self.model = Model(str(model_dir))
        self.wake_words = [w.lower() for w in cfg.get("voice.wake_words")]
        # Vosk only spots in-vocabulary phrases ("batsy" itself isn't one; "bat see"/"betsy" catch it).
        # The full list is still used to confirm the wake word in the Whisper transcript.
        grammar = self.wake_words
        if hasattr(self.model, "find_word"):  # vosk >= 0.3.45; older versions just log a warning
            grammar = [w for w in grammar if all(self.model.find_word(x) >= 0 for x in w.split())]
        self.wake = KaldiRecognizer(self.model, RATE, json.dumps(grammar + ["[unk]"]))
        self.q: queue.Queue[bytes] = queue.Queue()
        self.history = collections.deque(maxlen=FRAMES_PER_SEC * 3)  # pre-roll buffer
        self.noise = 200.0
        self.stream = sd.RawInputStream(
            samplerate=RATE, blocksize=BLOCK, dtype="int16", channels=1,
            device=cfg.get("voice.input_device"), callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):
        self.q.put(bytes(indata))

    def start(self) -> None:
        self.stream.start()

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()

    def drain(self) -> None:
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.wake.Reset()

    def read(self, timeout: float = 0.5) -> bytes | None:
        try:
            frame = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        self.history.append(frame)
        return frame

    def threshold(self) -> float:
        return max(self.noise * 3.0, 300.0)

    def _hit(self, text: str) -> bool:
        text = " " + text.lower() + " "
        return any(f" {w} " in text for w in self.wake_words)

    def wake_hit(self, frame: bytes, strict: bool = False) -> bool:
        """Feed a frame to the wake spotter. strict=True waits for a finished phrase (fewer false hits,
        used while Batsy is talking)."""
        level = rms(frame)
        if level < self.threshold():
            self.noise = 0.98 * self.noise + 0.02 * level
        if self.wake.AcceptWaveform(frame):
            hit = self._hit(json.loads(self.wake.Result()).get("text", ""))
        elif strict:
            return False
        else:
            hit = self._hit(json.loads(self.wake.PartialResult()).get("partial", ""))
        if hit:
            self.wake.Reset()
            log.info("wake word heard")
        return hit

    def preroll(self, seconds: float) -> list[bytes]:
        n = int(seconds * FRAMES_PER_SEC)
        return list(self.history)[-n:]

    def record_utterance(self, start_timeout: float, preroll: list[bytes] | None = None,
                         end_silence: float = 0.9, max_seconds: float = 30) -> np.ndarray | None:
        """Record until the speaker goes quiet. Returns int16 samples, or None if nobody spoke."""
        frames = list(preroll or [])
        thresh = self.threshold()
        started, silent, waited, total = False, 0, 0, 0
        while total < max_seconds * FRAMES_PER_SEC:
            frame = self.read(timeout=1.0)
            if frame is None:
                continue
            frames.append(frame)
            total += 1
            loud = rms(frame) > thresh
            if not started:
                if loud:
                    started, silent = True, 0
                else:
                    waited += 1
                    if waited > start_timeout * FRAMES_PER_SEC:
                        return None
                    if len(frames) > FRAMES_PER_SEC * 3 + len(preroll or []):
                        frames.pop(len(preroll or []))  # keep a short lead-in, not all the waiting
                continue
            silent = 0 if loud else silent + 1
            if silent > end_silence * FRAMES_PER_SEC:
                break
        self.wake.Reset()
        return np.frombuffer(b"".join(frames), dtype=np.int16)


def strip_wake_word(text: str, wake_words: list[str]) -> tuple[bool, str]:
    """Find the wake word near the start of a transcript. Returns (found, rest_of_command)."""
    import difflib

    words = text.split()
    norm = [re.sub(r"[^a-z]", "", w.lower()) for w in words]
    targets = [w.replace(" ", "") for w in wake_words]
    for i in range(min(len(words), 5)):
        for span in (2, 1):
            cand = "".join(norm[i:i + span])
            if cand and max(difflib.SequenceMatcher(None, cand, t).ratio() for t in targets) >= 0.7:
                rest = " ".join(words[i + span:]).lstrip(" ,.!?-")
                return True, rest
    return False, text
