"""A dedicated "Batsy" keyword spotter (sherpa-onnx KWS, ~3M parameters, ~2% CPU), tuned to your voice.

Vosk spots the wake word as a side effect of general recognition, so it fires on anything vaguely like "Batsy"
(three out of four triggers were false) and every hit needs a slower Parakeet check. This model listens for
the keyword only. When it fires, Batsy answers straight away; Vosk plus the check remain as a fallback.

"Train my wake word" records you saying "Batsy" a few times; tune() then picks the most sensitive setting that
still catches every one of them while staying silent on saved false alarms (wake_samples/*_false_*.wav).
"""
import json
import logging
import time
import wave
from pathlib import Path

import numpy as np

from batsy.config import DATA_DIR, MODELS_DIR

log = logging.getLogger("batsy.wake")
MODEL = MODELS_DIR / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
SETTINGS = DATA_DIR / "kws.json"
SAMPLES = DATA_DIR / "wake_samples"
KEEP_SAMPLES = 300
# "Batsy" the ways people say it, as the model's sub-word pieces
VARIANTS = ["▁BA T S Y", "▁BA T S I E", "▁BA T ▁SEE", "▁BA T S ▁SEE", "▁PA T S Y"]
DEFAULT = {"threshold": 0.2, "boost": 2.0}


def available() -> bool:
    return MODEL.exists()


def settings() -> dict:
    try:
        return {**DEFAULT, **json.loads(SETTINGS.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT)


def make(threshold: float | None = None, boost: float | None = None):
    import sherpa_onnx

    s = settings()
    thr = s["threshold"] if threshold is None else threshold
    bst = s["boost"] if boost is None else boost
    kw = DATA_DIR / "kws_keywords.txt"
    kw.write_text("".join(f"{v} :{bst} #{thr} @BATSY\n" for v in VARIANTS))
    f = lambda part: str(MODEL / f"{part}-epoch-12-avg-2-chunk-16-left-64.int8.onnx")  # noqa: E731
    return sherpa_onnx.KeywordSpotter(tokens=str(MODEL / "tokens.txt"), encoder=f("encoder"), decoder=f("decoder"),
                                      joiner=f("joiner"), keywords_file=str(kw), num_threads=1,
                                      keywords_score=bst, keywords_threshold=thr)


class Spotter:
    """Feed 16 kHz int16 frames; hit() is True on the frame where "Batsy" ends."""

    def __init__(self):
        self.kws = make()
        self.stream = self.kws.create_stream()

    def hit(self, frame: bytes) -> bool:
        self.stream.accept_waveform(16000, np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0)
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
        if self.kws.get_result(self.stream):
            self.kws.reset_stream(self.stream)
            return True
        return False

    def reset(self) -> None:
        self.kws.reset_stream(self.stream)


def count_hits(kws, audio: np.ndarray) -> int:
    s = kws.create_stream()
    a = audio.astype(np.float32) / 32768.0
    hits = 0
    for i in range(0, len(a), 1600):
        s.accept_waveform(16000, a[i:i + 1600])
        while kws.is_ready(s):
            kws.decode_stream(s)
        if kws.get_result(s):
            hits += 1
            kws.reset_stream(s)
    s.accept_waveform(16000, np.zeros(8000, dtype=np.float32))  # a little silence: let a final word finish
    while kws.is_ready(s):
        kws.decode_stream(s)
    return hits + (1 if kws.get_result(s) else 0)


def save_sample(audio: np.ndarray, label: str, source: str) -> None:
    """Keep wake-word audio (labelled real/false) for tuning. Local only; the newest KEEP_SAMPLES are kept."""
    try:
        SAMPLES.mkdir(parents=True, exist_ok=True)
        path = SAMPLES / f"{int(time.time() * 1000)}_{label}_{source}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(audio.astype(np.int16).tobytes())
        old = sorted(SAMPLES.glob("*.wav"))[:-KEEP_SAMPLES]
        for p in old:
            p.unlink(missing_ok=True)
    except OSError:
        pass


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def tune(positives: list[np.ndarray]) -> dict:
    """The least sensitive setting that still catches every recording of you saying "Batsy" (fewest false
    alarms), checked against saved false alarms. Saves it and returns {threshold, boost, caught, false}."""
    negatives = [_read(p) for p in sorted(SAMPLES.glob("*_false_*.wav"))[-60:]] if SAMPLES.exists() else []
    best = None
    for boost in (1.5, 2.5, 3.5):  # 12 settings: under a minute on this CPU
        for thr in (0.3, 0.2, 0.12, 0.06):
            kws = make(thr, boost)
            caught = sum(1 for a in positives if count_hits(kws, a) > 0)
            false = sum(count_hits(kws, a) for a in negatives)
            # prefer catching everything; then fewest false alarms; then the stricter (higher) threshold
            key = (caught, -false, thr, -boost)
            if best is None or key > best[0]:
                best = (key, {"threshold": thr, "boost": boost, "caught": caught, "of": len(positives),
                              "false": false, "negatives": len(negatives)})
    result = best[1]
    SETTINGS.write_text(json.dumps({"threshold": result["threshold"], "boost": result["boost"]}))
    log.info("wake word tuned: %s", result)
    return result
