"""The thinking part: a Claude agent loop. Memory is local (memory_store); each request carries only a
small context block, and messages within one request's tool loop are append-only."""
import getpass
import json
import logging
import os
import platform
import re
import threading
import time
from typing import Callable

import anthropic

from batsy.config import DATA_DIR, SOURCE_DIR, WORKSPACE
from batsy.tools import ToolRegistry
from batsy.memory_store import get_store
from batsy.tools import skills as skills_tool

log = logging.getLogger("batsy.brain")

BETAS = ["compact-2026-01-12"]
CLEAR_BETA = "context-management-2025-06-27"
USAGE_FILE = DATA_DIR / "usage.jsonl"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """\
You are Batsy, {user}'s voice assistant, running on their Fedora laptop with full control via tools \
(shell, files, system status, camera/screen, Arduino, timers/monitors, memory, self-written skills).

Talking
- Replies are spoken by TTS: 1-3 short sentences, no markdown/lists/emoji/URLs. Never read code or logs aloud; \
put code in files and say where.
- Input is speech transcripts; infer mistranscriptions ("our dino" = Arduino). Ask only if a wrong guess is costly.
- Don't say your own name (it's the wake word).

Acting
- Do things, don't explain how. Act, check briefly, report. Risky shell commands are voice-confirmed \
automatically, so don't ask separately.
- Be economical: fewest tool calls that do the job, quiet commands (e.g. `-q`, `| tail -20`, `head`), \
no re-reading what you already know.
- GUI apps and long commands: run_shell background=true (`gtk-launch firefox`, `xdg-open URL`). Root: \
`pkexec cmd` (password dialog; no sudo). Fedora: dnf, flatpak, `wpctl set-volume @DEFAULT_AUDIO_SINK@ 50%`, \
`brightnessctl set 40%`, `playerctl`, `nmcli`, `bluetoothctl`, `loginctl lock-session`, `gsettings`, `ydotool`.
- Camera: `camera open` shows the live feed in a window. Look at it (look source=webcam) only when the user asks \
something about what it shows, once per question; never comment on the feed unprompted. Screen questions: \
look source=screen.
- Music plays on the user's iPad through Spotify: use the spotify tool (not playerctl). For vague requests \
("something chill") play a fitting playlist.
- Arduino: write_sketch, compile, fix until it builds, upload, read serial to confirm. Workspace: {workspace}.
- "Tell me when..." = add_monitor / set_timer / background job. Use web_search for current facts.

Memory and self-improvement
- Memory is stored locally. Each message comes with what you remember, the recent conversation (with [did: ...] \
notes of the actions you took) and possibly related older exchanges, then [Now ...] and the request. Treat that as \
your memory. Use `recall` only if something you need is missing. Use `remember` for lasting facts about {user} \
(hardware, projects, preferences), unprompted when clear.
- To learn a new ability: create_skill, test with run_skill, fix until it works. Your source is {source}; edit \
only when asked, carefully, then restart_self.

Machine: {host} · {distro} · {desktop} · Python {python}
Skills:
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
        skills=skills_tool.list_skills(),
    )


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
        self.memory = get_store()
        self.messages: list[dict] = []  # the current request's tool loop only; rebuilt every request
        self.system = build_system_prompt()
        self.lock = threading.Lock()
        self.clear_tool_results = cfg.get("claude.clear_tool_results_at") > 0

    def note_announcement(self, text: str) -> None:
        self.memory.add_exchange("alert", "", text, [])

    def _request_kwargs(self) -> dict:
        c = self.cfg.get("claude")
        model = c["model"]
        tools = self.registry.definitions()
        if c["web_tools"]:
            tools += [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 6000},
            ]
        betas = list(BETAS)
        # Token savers: drop stale tool outputs (big shell dumps, old images) and summarise long history.
        edits = []
        if self.clear_tool_results:
            betas.append(CLEAR_BETA)
            edits.append({
                "type": "clear_tool_uses_20250919",
                "trigger": {"type": "input_tokens", "value": c["clear_tool_results_at"]},
                "keep": {"type": "tool_uses", "value": c["keep_tool_results"]},
            })
        edits.append({"type": "compact_20260112",
                      "trigger": {"type": "input_tokens", "value": max(50000, c["compact_at_tokens"])}})
        kwargs = dict(
            model=model,
            max_tokens=c["max_tokens"],
            # tools + system are identical every request: cache them for an hour
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral", "ttl": c["cache_ttl"]}}],
            tools=tools,
            output_config={"effort": c["effort"]},
            cache_control={"type": "ephemeral"},  # 5 min: covers the steps of one tool loop
            context_management={"edits": edits},
            betas=betas,
        )
        if model in FALLBACK_MODELS:
            kwargs["betas"].append(FALLBACK_BETA)
            kwargs["fallbacks"] = "default"
        return kwargs

    def _log_usage(self, msg) -> None:
        u = msg.usage
        row = {"t": int(time.time()), "model": msg.model, "in": u.input_tokens, "out": u.output_tokens,
               "cache_read": u.cache_read_input_tokens or 0, "cache_write": u.cache_creation_input_tokens or 0}
        log.info("usage %s", row)
        with open(USAGE_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")

    def _stream_once(self, chunker: SpeechChunker, abort: threading.Event):
        with self.client.beta.messages.stream(messages=self.messages, **self._request_kwargs()) as stream:
            for event in stream:
                if abort.is_set():
                    return None
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    chunker.feed(event.delta.text)
            final = stream.get_final_message()
        self._log_usage(final)
        return final

    def ask(self, text: str, source: str, say: Callable[[str], None], abort: threading.Event,
            on_tool: Callable[[str], None] | None = None) -> str:
        """Run one user request to completion. `say` receives speakable sentences as they stream."""
        with self.lock:
            return self._ask(text, source, say, abort, on_tool)

    def _ask(self, text, source, say, abort, on_tool) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M %a")
        context = self.memory.build_context(text, self.cfg)
        prompt = f"{context}\n\n[Now {stamp} · {source}] {text}" if context else f"[Now {stamp} · {source}] {text}"
        self.messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        actions: list[str] = []
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
                if self.clear_tool_results and "clear_tool_uses" in str(e):
                    # this account/model rejects tool-result clearing; carry on with compaction only
                    log.warning("disabling tool-result clearing: %s", e)
                    self.clear_tool_results = False
                    continue
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
            self.messages.append({"role": "assistant", "content": [b.to_dict() for b in final.content]})

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
                    actions.append(_action_note(tu.name, tu.input, content, is_error))
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
            if abort.is_set():
                break
        reply = " ".join(spoken) + (" (interrupted)" if abort.is_set() else "")
        self.memory.add_exchange(source, text, reply, actions)
        return " ".join(spoken)

    def reset(self) -> None:
        """'New conversation': the recent transcript starts empty; older exchanges stay searchable."""
        with self.lock:
            self.memory.new_session()
            self.system = build_system_prompt()


def _action_note(name: str, args: dict, content, is_error: bool) -> str:
    """One short line per tool call, so the next request knows what was done without the full output."""
    key = next((str(args[k]) for k in ("command", "path", "sketch", "name", "fact", "source", "action")
                if args.get(k)), "")
    if name == "arduino" and args.get("sketch"):
        key = f"{args.get('action')} {args['sketch']}"
    text = content if isinstance(content, str) else "image"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return f"{name} {' '.join(key.split())[:80]} -> {'ERROR ' if is_error else ''}{first[:70]}"
