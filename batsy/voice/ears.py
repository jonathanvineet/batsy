"""Microphone input: always-on wake word spotting (Vosk, offline) and utterance recording with a simple VAD."""
import collections
import json
import logging
import queue
import re
import time

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
        self.spotter = None  # the dedicated "Batsy" keyword spotter (batsy/voice/wake.py); Vosk is the fallback
        self.wake_source = None  # which one fired last: "kws" (precise, answer at once) or "vosk" (check first)
        try:
            from batsy.voice import wake

            if wake.available():
                self.spotter = wake.Spotter()
        except Exception as e:
            log.warning("keyword spotter unavailable (%s); using Vosk only", e)
        self.q: queue.Queue[bytes] = queue.Queue()
        self.history = collections.deque(maxlen=FRAMES_PER_SEC * 3)  # pre-roll buffer
        # Background level = a low percentile of the last few seconds, so it follows the room both ways
        # (a fan spinning up must raise it, or every frame looks like speech and recording never ends).
        self.levels = collections.deque(maxlen=FRAMES_PER_SEC * 5)
        self.stream = None
        self.last_frame = time.monotonic()
        self.next_retry = 0.0
        self._open()

    def _open(self) -> None:
        import sounddevice as sd

        self.stream = sd.RawInputStream(
            samplerate=RATE, blocksize=BLOCK, dtype="int16", channels=1,
            device=self.cfg.get("voice.input_device"), callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):
        self.last_frame = time.monotonic()
        self.q.put(bytes(indata))

    def start(self) -> None:
        self.stream.start()
        self.last_frame = time.monotonic()

    def close(self) -> None:
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    def reopen(self) -> bool:
        """The microphone went quiet (suspend/resume, USB mic replugged, PipeWire restarted): start it again.
        PortAudio only sees new devices after a re-init, so do that too."""
        import sounddevice as sd

        log.warning("microphone stopped delivering audio; reopening it")
        self.close()
        try:
            sd._terminate()
            sd._initialize()
            self._open()
            self.start()
            log.info("microphone reopened")
            return True
        except Exception as e:
            log.error("microphone still unavailable: %s", e)
            self.next_retry = time.monotonic() + 5
            return False

    def _watchdog(self) -> None:
        now = time.monotonic()
        if now - self.last_frame > 4 and now >= self.next_retry:
            self.next_retry = now + 5
            self.reopen()

    def drain(self) -> None:
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.wake.Reset()
        if self.spotter is not None:
            self.spotter.reset()

    def read(self, timeout: float = 0.5) -> bytes | None:
        try:
            frame = self.q.get(timeout=timeout)
        except queue.Empty:
            self._watchdog()
            return None
        self.history.append(frame)
        self.levels.append(rms(frame))
        return frame

    def threshold(self) -> float:
        noise = float(np.percentile(self.levels, 20)) if len(self.levels) >= FRAMES_PER_SEC else 200.0
        return max(noise * 3.0, 300.0)

    def _hit(self, text: str) -> bool:
        text = " " + text.lower() + " "
        return any(f" {w} " in text for w in self.wake_words)

    def wake_hit(self, frame: bytes, strict: bool = False) -> bool:
        """Feed a frame to the wake spotter. strict=True waits for a finished phrase (fewer false hits,
        used while Batsy is talking)."""
        if self.spotter is not None and self.spotter.hit(frame):
            self.wake.Reset()
            self.wake_source = "kws"
            log.info("wake word heard (keyword spotter)")
            return True
        self.wake_source = "vosk"
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

    def live_recognizer(self):
        """A full-vocabulary recognizer for showing words on screen as they're spoken."""
        from vosk import KaldiRecognizer

        return KaldiRecognizer(self.model, RATE)

    def record_utterance(self, start_timeout: float, preroll: list[bytes] | None = None,
                         end_silence: float | None = None, max_seconds: float = 15,
                         on_frame=None, min_speech: float = 0.0) -> np.ndarray | None:
        """Record until the speaker goes quiet. Returns int16 samples, or None if nobody spoke.
        on_frame(frame, level, speaking) is called for every frame (drives the overlay).
        min_speech: until this many seconds of speech are heard, a pause has to be longer (1.6 s) to end the
        recording. Right after the wake word that's just the tail of "Batsy": people pause, then say the command."""
        frames = list(preroll or [])
        end_silence = end_silence or self.cfg.get("voice.end_silence")
        thresh = self.threshold()
        started, silent, waited, total, spoken = False, 0, 0, 0, 0
        while total < max_seconds * FRAMES_PER_SEC:
            frame = self.read(timeout=1.0)
            if frame is None:
                continue
            frames.append(frame)
            total += 1
            level = rms(frame)
            loud = level > thresh
            if on_frame:
                on_frame(frame, level / (thresh * 6), started or loud)
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
            spoken += loud
            pause = end_silence if spoken >= min_speech * FRAMES_PER_SEC else max(end_silence, 1.6)
            if silent > pause * FRAMES_PER_SEC:
                break
        self.wake.Reset()
        return np.frombuffer(b"".join(frames), dtype=np.int16)


# How speech recognizers (Parakeet especially) spell "Batsy". Only used to confirm a wake word Vosk already
# spotted, never to wake Batsy, so everyday phrases like "that's it" can't start a conversation on their own.
TRANSCRIPT_ALIASES = ["thats it", "thatsy", "matty", "matsy", "latzy", "lattsy", "patsy", "batty", "baxy", "bassy",
                      "betsy", "bitsy", "pasty"]


def strip_wake_word(text: str, wake_words: list[str]) -> tuple[bool, str]:
    """Find the wake word near the start of a transcript. Returns (found, rest_of_command)."""
    import difflib

    words = text.split()
    norm = [re.sub(r"[^a-z]", "", w.lower()) for w in words]
    targets = [(w.replace(" ", ""), 0.7) for w in wake_words]
    # Recogniser spellings only count when nearly exact: "that" must not pass for "that's it".
    targets += [(w.replace(" ", ""), 0.86) for w in TRANSCRIPT_ALIASES]

    def score(cand: str) -> float:
        """How far past its own threshold the best target is (>= 0 means it's the wake word)."""
        return max(difflib.SequenceMatcher(None, cand, t).ratio() - need for t, need in targets) if cand else -1.0

    for i in range(min(len(words), 5)):
        # Take two words only when they match better than one ("bat see"), not "Batsy, what" -> "batsywhat".
        best, span = max((score("".join(norm[i:i + n])), -n) for n in (1, 2))
        if best >= 0:
            return True, " ".join(words[i - span:]).lstrip(" ,.!?-")
    return False, text
