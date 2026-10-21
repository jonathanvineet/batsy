"""Streaming text -> speakable sentences, with inline screen markers cut out. Shared by every brain."""
import re
from typing import Callable


MARKER = re.compile(r"\[\[(show|card|pull|open):\s*(.*?)\]\]", re.S)


class SpeechChunker:
    """Turns streamed text into speakable sentences, skipping code blocks.

    Inline visual markers ([[show: query]], [[card: Title | text]]) are cut out of the speech and handed
    to on_marker as soon as they're complete, so pictures appear while Batsy is still talking and
    without an extra tool round trip."""

    def __init__(self, emit: Callable[[str], None], on_marker: Callable[[str, str], None] | None = None):
        self.emit, self.on_marker, self.buf, self.in_code = emit, on_marker, "", False

    def _pull_markers(self) -> None:
        while (m := MARKER.search(self.buf)):
            if self.on_marker:
                self.on_marker(m.group(1), m.group(2).strip())
            self.buf = self.buf[:m.start()] + self.buf[m.end():]

    def feed(self, text: str) -> None:
        self.buf += text
        self._pull_markers()
        while True:
            if self.in_code:
                end = self.buf.find("```")
                if end < 0:
                    return
                self.buf, self.in_code = self.buf[end + 3:], False
                continue
            pending = self.buf.find("[[")  # a marker still streaming in: don't speak past it
            region = self.buf if pending < 0 else self.buf[:pending]
            fence = region.find("```")
            # first sentence boundary that yields a chunk long enough not to sound choppy
            m = next((b for b in re.finditer(r"(?<=[.!?:;])\s+|\n+", region)
                      if b.start() >= 25 or "\n" in b.group()), None)
            if fence >= 0 and (m is None or fence < m.start()):
                self._say(self.buf[:fence])
                self.buf, self.in_code = self.buf[fence + 3:], True
                continue
            if m is None:
                return
            self._say(self.buf[:m.start()])
            self.buf = self.buf[m.end():]

    def flush(self) -> None:
        self._pull_markers()
        if not self.in_code:
            self._say(self.buf.split("[[", 1)[0])  # drop an unfinished marker
        self.buf, self.in_code = "", False

    def _say(self, text: str) -> None:
        text = re.sub(r"https?://\S+", "a link", text)
        text = re.sub(r"[*_#`>|]+", "", text)
        text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M).strip()
        if re.search(r"\w", text):
            self.emit(text)
