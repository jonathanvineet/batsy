"""The thinking part: a Claude agent loop with persistent, append-only conversation memory."""
import getpass
import json
import logging
import os
import platform
import re
import shutil
import threading
import time
from typing import Callable

import anthropic

from batsy.config import DATA_DIR, SOURCE_DIR, WORKSPACE
from batsy.tools import ToolRegistry
from batsy.tools import memory as memory_tool
from batsy.tools import skills as skills_tool

log = logging.getLogger("batsy.brain")

HISTORY_FILE = DATA_DIR / "history.json"
ARCHIVE_DIR = DATA_DIR / "archive"
BETAS = ["compact-2026-01-12"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """\
You are Batsy, {user}'s personal voice assistant. You run as a program on their Fedora Linux laptop and have \
full control of it through your tools: a bash shell, file access, live system monitoring, the screen and webcam, \
Arduino boards, background monitors and timers, long-term memory, and skills you write for yourself.

How you talk
- Almost everything you say is spoken aloud by text-to-speech. Keep replies short and natural: one to three \
sentences unless asked for detail. No markdown, bullet lists, tables, emoji or URLs in spoken replies.
- Never read code, logs or long paths aloud. Write code to files and say where it is and what it does.
- Messages are speech transcripts and may contain mistranscriptions; infer the likely meaning (e.g. "our dino" \
is probably "Arduino"). If a request is truly ambiguous and acting wrongly would be costly, ask one short question.
- Don't say your own name in replies (it is your wake word).
- Each user message starts with a bracketed timestamp and channel, e.g. [2026-10-21 14:03 Tue · voice]. Lines \
starting "[Batsy announced]" are things you said on your own (alerts, timers) since the last message.

How you act
- You're expected to actually do things, not explain how. Use tools, check the result, then report briefly.
- Prefer doing over asking. Dangerous shell commands are automatically confirmed with the user by voice, so you \
don't need to ask permission separately for them.
- Launch GUI apps and long-running commands with run_shell background=true (e.g. `gtk-launch firefox`, \
`xdg-open URL`, `code ~/project`). Fedora specifics: dnf for packages (via pkexec), flatpak, `wpctl set-volume \
@DEFAULT_AUDIO_SINK@ 50%`, `wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle`, `brightnessctl set 40%`, `playerctl \
play-pause|next|metadata`, `nmcli`, `bluetoothctl`, `loginctl lock-session`, `gsettings`, `notify-send`, \
`systemctl --user`, `journalctl`. Keyboard/mouse automation on Wayland: `ydotool` (needs ydotoold running).
- Root access: `pkexec <command>` shows the user a password dialog. There is no terminal for sudo passwords.
- Use `look` to see the screen when the user refers to what's on it, or to verify GUI actions.
- Your workspace for projects, sketches and scripts is {workspace}.
- Arduino: write the sketch with the arduino tool (write_sketch), compile, fix errors yourself until it compiles, \
then upload; read the serial port to confirm it works. Install missing cores/libraries yourself.
- For "tell me when..." requests use add_monitor or set_timer; for long builds use background jobs.
- Use web_search / web_fetch for anything current or that you don't know.

Memory
- This conversation persists across restarts, so earlier turns are real history with the user.
- Use `remember` for lasting facts about {user}, their hardware, projects and preferences, without being asked \
when something is clearly worth keeping.

Self-improvement
- When asked to learn something new or automate a repeated task, write a skill (create_skill), test it with \
run_skill, and fix it until it works.
- Your own source code is at {source}. You may edit it when asked to change your own behaviour; keep edits \
small and careful, then call restart_self.

Machine: {host} · {distro} · desktop {desktop} · Python {python}

Long-term memory (fact ids in brackets):
{memory}

Skills you have written:
{skills}
"""


def _distro() -> str:
    try:
        info = platform.freedesktop_os_release()
        return info.get("PRETTY_NAME", "Linux")
    except OSError:
        return platform.platform()


def build_system_prompt() -> str:
    return SYSTEM_PROMPT.format(
        user=os.environ.get("BATSY_USER_NAME") or getpass.getuser(),
        workspace=WORKSPACE,
        source=SOURCE_DIR,
        host=platform.node(),
        distro=_distro(),
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", "unknown") + "/" + os.environ.get("XDG_SESSION_TYPE", "?"),
        python=platform.python_version(),
        memory=memory_tool.as_prompt(),
        skills=skills_tool.list_skills(),
    )


class History:
    """The conversation, persisted after every change. Only ever appended to: rewriting earlier turns
    would invalidate the model's preserved thinking."""

    def __init__(self, path=HISTORY_FILE):
        self.path = path
        self.messages: list[dict] = []
        if path.exists():
            try:
                self.messages = json.loads(path.read_text())
            except ValueError:
                log.warning("history file unreadable; archiving and starting fresh")
                self.archive()
        self._repair()

    def _repair(self) -> None:
        # A crash between a tool call and its result leaves dangling tool_use blocks; answer them.
        if self.messages and self.messages[-1]["role"] == "assistant":
            pending = [b for b in self.messages[-1]["content"] if b.get("type") == "tool_use"]
            if pending:
                self.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                     "content": "Interrupted: Batsy restarted before this ran."} for b in pending
                ]})

    def append(self, message: dict) -> None:
        self.messages.append(message)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.messages))
        os.replace(tmp, self.path)

    def archive(self) -> None:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.move(self.path, ARCHIVE_DIR / f"history-{time.strftime('%Y%m%d-%H%M%S')}.json")
        self.messages = []


class SpeechChunker:
    """Turns streamed text into speakable sentences, skipping code blocks."""

    def __init__(self, emit: Callable[[str], None]):
        self.emit, self.buf, self.in_code = emit, "", False

    def feed(self, text: str) -> None:
        self.buf += text
        while True:
            if self.in_code:
                end = self.buf.find("```")
                if end < 0:
                    return
                self.buf, self.in_code = self.buf[end + 3:], False
                continue
            fence = self.buf.find("```")
            # first sentence boundary that yields a chunk long enough not to sound choppy
            m = next((b for b in re.finditer(r"(?<=[.!?:;])\s+|\n+", self.buf)
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
        if not self.in_code:
            self._say(self.buf)
        self.buf, self.in_code = "", False

    def _say(self, text: str) -> None:
        text = re.sub(r"https?://\S+", "a link", text)
        text = re.sub(r"[*_#`>|]+", "", text)
        text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M).strip()
        if re.search(r"\w", text):
            self.emit(text)


class Brain:
    def __init__(self, cfg, registry: ToolRegistry):
        self.cfg = cfg
        self.registry = registry
        self.client = anthropic.Anthropic()
        self.history = History()
        self.system = build_system_prompt()
        self.lock = threading.Lock()
        self.notes: list[str] = []  # announcements made since the last user message

    def note_announcement(self, text: str) -> None:
        self.notes.append(text)

    def _request_kwargs(self) -> dict:
        model = self.cfg.get("claude.model")
        tools = self.registry.definitions()
        if self.cfg.get("claude.web_tools"):
            tools += [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
            ]
        kwargs = dict(
            model=model,
            max_tokens=self.cfg.get("claude.max_tokens"),
            system=[{"type": "text", "text": self.system}],
            tools=tools,
            output_config={"effort": self.cfg.get("claude.effort")},
            cache_control={"type": "ephemeral"},
            context_management={"edits": [{"type": "compact_20260112"}]},
            betas=list(BETAS),
        )
        if model in FALLBACK_MODELS:
            kwargs["betas"].append(FALLBACK_BETA)
            kwargs["fallbacks"] = "default"
        return kwargs

    def _stream_once(self, chunker: SpeechChunker, abort: threading.Event):
        with self.client.beta.messages.stream(messages=self.history.messages, **self._request_kwargs()) as stream:
            for event in stream:
                if abort.is_set():
                    return None
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    chunker.feed(event.delta.text)
            return stream.get_final_message()

    def ask(self, text: str, source: str, say: Callable[[str], None], abort: threading.Event,
            on_tool: Callable[[str], None] | None = None) -> str:
        """Run one user request to completion. `say` receives speakable sentences as they stream."""
        with self.lock:
            return self._ask(text, source, say, abort, on_tool)

    def _ask(self, text, source, say, abort, on_tool) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M %a")
        notes = "".join(f"[Batsy announced] {n}\n" for n in self.notes)
        self.notes.clear()
        self.history.append({"role": "user", "content": [{"type": "text", "text": f"{notes}[{stamp} · {source}] {text}"}]})

        spoken: list[str] = []

        def emit(sentence: str) -> None:
            spoken.append(sentence)
            say(sentence)

        for _ in range(self.cfg.get("claude.max_steps")):
            chunker = SpeechChunker(emit)
            try:
                final = self._stream_once(chunker, abort)
            except anthropic.AuthenticationError:
                emit("My Claude API key was rejected. Please check the key in the Batsy env file.")
                break
            except anthropic.RateLimitError:
                emit("Claude is rate limiting me right now. Try again in a minute.")
                break
            except anthropic.BadRequestError as e:
                log.error("bad request: %s", e)
                emit("Claude rejected that request. If this keeps happening, say: new conversation.")
                break
            except anthropic.APIStatusError as e:
                log.error("api error %s: %s", e.status_code, e)
                emit(f"Claude had a server error, code {e.status_code}. Try again shortly.")
                break
            except anthropic.APIConnectionError:
                emit("I can't reach Claude. Check the internet connection.")
                break
            if final is None:  # interrupted
                break
            chunker.flush()
            self.history.append({"role": "assistant", "content": [b.to_dict() for b in final.content]})

            if final.stop_reason == "refusal":
                emit("I can't help with that one.")
                break
            if final.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in final.content if b.type == "tool_use"]
            if not tool_uses:
                break

            results = []
            for tu in tool_uses:
                if final.stop_reason == "max_tokens":
                    content, is_error = "Output was cut off by max_tokens before this call finished; re-issue it.", True
                elif abort.is_set():
                    content, is_error = "Cancelled: the user interrupted.", True
                else:
                    if on_tool:
                        on_tool(tu.name)
                    log.info("tool %s %s", tu.name, json.dumps(tu.input)[:300])
                    content, is_error = self.registry.execute(tu.name, tu.input)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error})
            self.history.append({"role": "user", "content": results})
            if abort.is_set():
                break
        return " ".join(spoken)

    def reset(self) -> None:
        with self.lock:
            self.history.archive()
            self.system = build_system_prompt()
