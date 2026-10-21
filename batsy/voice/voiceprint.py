"""Speaker check: Batsy only answers the enrolled voice, so song vocals, the TV, or its own voice coming back
through the mic don't trigger it. Local (sherpa-onnx + a WeSpeaker model); off until you say "learn my voice"."""
import logging

import numpy as np

from batsy.config import DATA_DIR, MODELS_DIR

log = logging.getLogger("batsy.voiceprint")

PRINT_PATH = DATA_DIR / "voiceprint.npy"


class VoicePrint:
    def __init__(self, cfg):
        self.cfg = cfg
        self.extractor = None
        self.owner: np.ndarray | None = None
        model = MODELS_DIR / cfg.get("voice.speaker_model")
        if not model.exists():
            log.info("speaker model not at %s; answering any voice", model)
            return
        try:
            import sherpa_onnx

            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2))
        except Exception as e:
            log.warning("speaker check unavailable (%s)", e)
            return
        if PRINT_PATH.exists():
            self.owner = np.load(PRINT_PATH)

    @property
    def available(self) -> bool:
        return self.extractor is not None

    @property
    def active(self) -> bool:
        return self.available and self.owner is not None

    def embed(self, audio: np.ndarray) -> np.ndarray:
        s = self.extractor.create_stream()
        s.accept_waveform(sample_rate=16000, waveform=audio.astype(np.float32) / 32768.0)
        s.input_finished()
        v = np.array(self.extractor.compute(s), dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    def is_owner(self, audio: np.ndarray | None) -> bool:
        """True if this sounds like you (or no voice is enrolled)."""
        if not self.active or audio is None or audio.size < 16000 // 2:
            return True
        score = float(self.embed(audio) @ self.owner)
        ok = score >= self.cfg.get("voice.speaker_match")
        log.info("speaker score %.2f (%s)", score, "you" if ok else "not you; ignoring")
        return ok

    def enroll(self, clips: list[np.ndarray]) -> None:
        v = np.mean([self.embed(c) for c in clips], axis=0)
        self.owner = v / (np.linalg.norm(v) + 1e-9)
        np.save(PRINT_PATH, self.owner)

    def forget(self) -> None:
        self.owner = None
        PRINT_PATH.unlink(missing_ok=True)
