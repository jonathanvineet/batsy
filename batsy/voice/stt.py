"""Speech-to-text: faster-whisper (accurate, local) or Vosk (light fallback)."""
import json
import logging

import numpy as np

log = logging.getLogger("batsy.stt")

PROMPT = ("Batsy, Arduino, Fedora, terminal, sketch, upload, compile, serial monitor, ESP32, Uno, Nano, "
          "Python, GitHub, VS Code, Firefox, CPU, RAM.")


class Transcriber:
    def __init__(self, cfg, vosk_model=None):
        self.engine = None
        want = cfg.get("voice.stt_engine")
        if want in ("auto", "whisper"):
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

    def transcribe(self, audio: np.ndarray) -> str:
        if audio is None or audio.size == 0:
            return ""
        if self.engine == "whisper":
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
