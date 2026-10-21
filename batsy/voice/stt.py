"""Speech-to-text, all local: Parakeet via sherpa-onnx (most accurate and fastest on CPU), faster-whisper,
or Vosk (light fallback)."""
import json
import logging

import numpy as np

from batsy.config import DATA_DIR, MODELS_DIR

log = logging.getLogger("batsy.stt")

PROMPT = ("Batsy, Arduino, Fedora, terminal, sketch, upload, compile, serial monitor, ESP32, Uno, Nano, "
          "Python, GitHub, VS Code, Firefox, CPU, RAM.")


class Transcriber:
    def __init__(self, cfg, vosk_model=None):
        self.engine = None
        want = cfg.get("voice.stt_engine")
        if want in ("auto", "parakeet"):
            self._load_parakeet(cfg)
        if self.engine is None and want in ("auto", "whisper", "parakeet"):
            try:
                from faster_whisper import WhisperModel

                name = cfg.get("voice.whisper_model")
                log.info("loading whisper %s", name)
                self.whisper = WhisperModel(name, device="cpu", compute_type="int8")
                self.engine = "whisper"
            except Exception as e:
                log.warning("faster-whisper unavailable (%s); falling back to vosk", e)
        if self.engine is None:
            if vosk_model is None:
                raise RuntimeError("No speech-to-text engine available")
            self.vosk_model = vosk_model
            self.engine = "vosk"
        log.info("STT engine: %s", self.engine)

    def _load_parakeet(self, cfg) -> None:
        d = MODELS_DIR / cfg.get("voice.parakeet_model")
        if not d.exists():
            log.info("Parakeet model not at %s; using whisper", d)
            return
        try:
            import sherpa_onnx

            log.info("loading parakeet %s", d.name)
            f = lambda stem: str(next(d.glob(f"{stem}*.onnx")))  # noqa: E731  (int8 or fp32 file names)
            model = dict(encoder=f("encoder"), decoder=f("decoder"), joiner=f("joiner"), tokens=str(d / "tokens.txt"),
                         model_type="nemo_transducer", num_threads=4)
            try:
                self.parakeet = sherpa_onnx.OfflineRecognizer.from_transducer(**model, **self._hotwords(cfg, d))
            except Exception as e:
                log.warning("Parakeet hotwords unavailable (%s); plain decoding", e)
                self.parakeet = sherpa_onnx.OfflineRecognizer.from_transducer(**model)
            self.engine = "parakeet"
        except Exception as e:
            log.warning("Parakeet unavailable (%s); trying whisper", e)

    @staticmethod
    def _hotwords(cfg, d) -> dict:
        """Bias Parakeet toward words it doesn't know ("Batsy", "Arduino"), like Whisper's PROMPT does."""
        words = cfg.get("voice.hotwords")
        if not words:
            return {}
        vocab = d / "bpe.vocab"  # sherpa-onnx encodes hotwords with this; Parakeet ships only tokens.txt
        if not vocab.exists():
            lines = (ln.rsplit(" ", 1) for ln in (d / "tokens.txt").read_text().splitlines())
            vocab.write_text("".join(f"{piece}\t{-int(i)}\n" for piece, i in lines))
        hot = DATA_DIR / "hotwords.txt"
        hot.write_text("\n".join(words) + "\n")
        return dict(decoding_method="modified_beam_search", hotwords_file=str(hot),
                    hotwords_score=cfg.get("voice.hotwords_score"), modeling_unit="bpe", bpe_vocab=str(vocab))

    def transcribe(self, audio: np.ndarray) -> str:
        if audio is None or audio.size == 0:
            return ""
        if self.engine == "parakeet":
            stream = self.parakeet.create_stream()
            stream.accept_waveform(16000, audio.astype(np.float32) / 32768.0)
            self.parakeet.decode_stream(stream)
            text = stream.result.text.strip()
        elif self.engine == "whisper":
            segments, _ = self.whisper.transcribe(
                audio.astype(np.float32) / 32768.0, language="en", beam_size=3,
                initial_prompt=PROMPT, vad_filter=True, condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        else:
            from vosk import KaldiRecognizer

            rec = KaldiRecognizer(self.vosk_model, 16000)
            rec.AcceptWaveform(audio.tobytes())
            text = json.loads(rec.FinalResult()).get("text", "")
        log.info("heard: %s", text)
        return text
