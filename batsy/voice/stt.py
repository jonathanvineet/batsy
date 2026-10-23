"""Speech-to-text, all local: Parakeet via sherpa-onnx (most accurate and fastest on CPU), faster-whisper,
or Vosk (light fallback)."""
import json
import logging
import re

import numpy as np

from batsy import lexicon
from batsy.config import DATA_DIR, MODELS_DIR

log = logging.getLogger("batsy.stt")

PROMPT = ("Batsy, Arduino, Fedora, terminal, sketch, upload, compile, serial monitor, ESP32, Uno, Nano, "
          "Python, GitHub, VS Code, Firefox, CPU, RAM.")


class Transcriber:
    def __init__(self, cfg, vosk_model=None):
        self.engine = None
        self.cfg = cfg
        self.prompt = lexicon.whisper_prompt(cfg, PROMPT)  # your everyday words, so Whisper expects them
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
        """Bias Parakeet toward words it doesn't know ("Batsy", "PETG", your project names), like Whisper's prompt.
        The list comes from batsy/lexicon.py: the config's words, your domains, and your projects."""
        pieces = [ln.rsplit(" ", 1) for ln in (d / "tokens.txt").read_text().splitlines()]
        chars = set("".join(p for p, _ in pieces).replace("\u2581", "")) | {" "}
        # a hotword the model can't spell (a character missing from its tokens) would break hotwords for all
        words = [w for w in (" ".join(re.sub(r"[^\w' ]", " ", w).split()) for w in lexicon.hotwords(cfg))
                 if w and set(w) <= chars]
        if not words:
            return {}
        vocab = d / "bpe.vocab"  # sherpa-onnx encodes hotwords with this; Parakeet ships only tokens.txt
        if not vocab.exists():
            vocab.write_text("".join(f"{piece}\t{-int(i)}\n" for piece, i in pieces))
        hot = DATA_DIR / "hotwords.txt"
        hot.write_text("\n".join(words) + "\n")
        return dict(decoding_method="modified_beam_search", hotwords_file=str(hot),
                    hotwords_score=cfg.get("voice.hotwords_score"), modeling_unit="bpe", bpe_vocab=str(vocab))

    def live(self, preroll: list[bytes] | None = None) -> "LiveTranscript":
        """Transcribe while you're still talking (Parakeet only): see LiveTranscript."""
        return LiveTranscript(self, preroll)

    def raw(self, audio: np.ndarray) -> str:
        """Just the model's transcript of some audio, nothing fixed up."""
        stream = self.parakeet.create_stream()
        stream.accept_waveform(16000, audio.astype(np.float32) / 32768.0)
        self.parakeet.decode_stream(stream)
        return stream.result.text.strip()

    def transcribe(self, audio: np.ndarray, quiet: bool = False, live: "LiveTranscript | None" = None) -> str:
        if audio is None or audio.size == 0:
            return ""
        done = live.finish(len(audio)) if live is not None and self.engine == "parakeet" else None
        if done is not None:  # most of it was transcribed while you were talking
            text = done
        elif self.engine == "parakeet":
            stream = self.parakeet.create_stream()
            stream.accept_waveform(16000, audio.astype(np.float32) / 32768.0)
            self.parakeet.decode_stream(stream)
            text = stream.result.text.strip()
        elif self.engine == "whisper":
            segments, _ = self.whisper.transcribe(
                audio.astype(np.float32) / 32768.0, language="en", beam_size=3,
                initial_prompt=self.prompt, vad_filter=True, condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        else:
            from vosk import KaldiRecognizer

            rec = KaldiRecognizer(self.vosk_model, 16000)
            rec.AcceptWaveform(audio.tobytes())
            text = json.loads(rec.FinalResult()).get("text", "")
        if not quiet:  # (wake-word checks stay raw)
            text = self._by_topic(audio, text)
            fixed = lexicon.fix(text, self.cfg)
            log.info("heard: %s%s", text, f"  ->  {fixed}" if fixed != text else "")
            text = fixed
        return text

    def _by_topic(self, audio: np.ndarray, text: str) -> str:
        """Music or a command (voice/topics.py): decode again, biased to that topic's words that sound closest to
        what was heard, then repair what still came out wrong ("pseudo" -> "sudo"). ~0.5 s, only for these."""
        from batsy.voice import topics

        kind = topics.topic(text)
        if not kind:
            return text
        first = text
        try:
            # A second decode costs as much as the first: only for music the first one didn't match well.
            # Commands are fixed by repair() alone (milliseconds).
            needed = kind == "music" and topics.music_fit(text) < 90
            hw = topics.hotwords(kind, text) if needed and self.engine == "parakeet" else ""
            if hw:
                stream = self.parakeet.create_stream(hotwords=hw)
                stream.accept_waveform(16000, audio.astype(np.float32) / 32768.0)
                self.parakeet.decode_stream(stream)
                second = stream.result.text.strip()
                # keep the biased decode only if it's still the same kind of request and, for music, it now
                # matches a real song or artist better than the first decode did
                if topics.topic(second) == kind and (kind != "music" or
                                                     topics.music_fit(second) > topics.music_fit(text) + 5):
                    text = second
            text = topics.repair(kind, text)
        except Exception as e:
            log.warning("topic pass failed: %s", e)
            return first
        if text != first:
            log.info("heard (%s): %s  ->  %s", kind, first, text)
        return text


class LiveTranscript:
    """Transcribes a recording in pieces while it's still being made. Each time you pause (0.3 s) after at least a
    second of speech, the audio so far goes to a background worker; when you stop, only the last stretch is left
    to transcribe. Pauses are natural word boundaries, so words aren't cut in half."""

    PAUSE_FRAMES = 8   # 0.4 s of quiet (50 ms frames): a real pause, not a gap between syllables
    MIN_FRAMES = 40    # at least 2 s per piece: the model needs some context to get words right

    def __init__(self, stt: Transcriber, preroll: list[bytes] | None):
        from concurrent.futures import ThreadPoolExecutor

        self.stt = stt
        self.frames: list[bytes] = list(preroll or [])
        self.cut = 0           # frames before this index are sent off
        self.quiet = 0
        self.spoke = False
        self.pieces = []       # futures, in order
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-stt")

    def feed(self, frame: bytes, loud: bool) -> None:
        self.frames.append(frame)
        self.quiet = 0 if loud else self.quiet + 1
        self.spoke = self.spoke or loud
        pending = len(self.frames) - self.cut
        if self.spoke and self.quiet >= self.PAUSE_FRAMES and pending >= self.MIN_FRAMES:
            end = len(self.frames) - self.quiet // 2  # cut in the middle of the pause
            self._send(self.cut, end)
            self.cut, self.spoke = end, False

    def _send(self, start: int, end: int) -> None:
        audio = np.frombuffer(b"".join(self.frames[start:end]), dtype=np.int16)
        self.pieces.append(self.pool.submit(self.stt.raw, audio))

    def finish(self, samples: int) -> str | None:
        """The whole transcript, or None if this doesn't cover the same audio (then decode it normally)."""
        if sum(len(f) for f in self.frames) // 2 != samples:
            self.pool.shutdown(wait=False, cancel_futures=True)
            return None
        if self.cut < len(self.frames):
            self._send(self.cut, len(self.frames))
        try:
            text = " ".join(p.result(timeout=30) for p in self.pieces)
        finally:
            self.pool.shutdown(wait=False)
        return " ".join(text.split())
