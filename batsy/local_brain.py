"""Local brain: a small model running in Ollama on this laptop. Free, offline, no limits.

Tuned for speed on a CPU with 8 GB RAM: a ~3B model kept loaded, a tiny system prompt (prompt reading is
the slow part on CPU), few compact tools, short tool outputs, and replies streamed straight into speech.
"Ask Claude ..." hands a request to Claude Code on the user's Pro plan, only when they say so.
"""
import getpass
import json
import re
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from batsy.brain import SpeechChunker
from batsy.config import WORKSPACE
from batsy.memory_store import get_store
from batsy.tools import ToolRegistry, truncate

log = logging.getLogger("batsy.local")

SYSTEM = """You are Batsy, {user}'s voice assistant on their Fedora laptop. Talk like a friend in a real \
conversation: warm, quick, natural, usually one or two short sentences. No lists, markdown or emoji. Input is \
speech-to-text, so guess sensibly at misheard words.
Use tools to actually do things on the laptop, then confirm in a few words. Open apps with run_shell \
background=true (e.g. gtk-launch firefox, xdg-open URL). Music plays on the iPad: use spotify.
Whenever the user wants to see something, however they phrase it, write [[show: exact name of the thing]] for a \
picture, or [[pull: exact name]] to put a person, place or thing on screen with its facts. For a list or steps \
write [[card: Title | line one; line two]]. Markers are not spoken.
If a request needs real expertise, careful reasoning, code, or you are not sure of the facts, call ask_claude \
with the full request instead of answering yourself.
Earlier conversation and things you remember come before [Now ...] in the message."""

# Compact tool set for the small model: (description, properties, required). Executed by the normal registry.
LOCAL_TOOLS = {
    "run_shell": ("Run a bash command on the laptop. background=true for apps and long tasks.",
                  {"command": {"type": "string"}, "background": {"type": "boolean"}}, ["command"]),
    "system_status": ("CPU, memory, temperature, battery, disk, network, top processes.",
                      {"sections": {"type": "array", "items": {"type": "string",
                       "enum": ["cpu", "memory", "temps", "battery", "disks", "network", "processes"]}}}, []),
    "spotify": ("Music on the iPad. action: play (query), queue, pause, resume, next, previous, now_playing, like.",
                {"action": {"type": "string", "enum": ["play", "queue", "pause", "resume", "next", "previous",
                                                       "now_playing", "like"]},
                 "query": {"type": "string"}}, ["action"]),
    "set_timer": ("Timer or reminder; message is spoken when it fires.",
                  {"seconds": {"type": "number"}, "message": {"type": "string"}}, ["seconds", "message"]),
    "agenda": ("The user's plans. add: date YYYY-MM-DD + text. list: date (and end_date).",
               {"action": {"type": "string", "enum": ["add", "list"]}, "date": {"type": "string"},
                "end_date": {"type": "string"}, "text": {"type": "string"}}, ["action"]),
    "remember": ("Save a lasting fact about the user.", {"fact": {"type": "string"}}, ["fact"]),
    "camera": ("Open or close the live camera window.",
               {"action": {"type": "string", "enum": ["open", "close"]}}, ["action"]),
    "ask_claude": ("Hand a hard request to Claude (expert answers, code, careful reasoning). Claude replies to the user.",
                   {"request": {"type": "string"}}, ["request"]),
}

# Requests that a 3B model would fumble: route straight to Claude without asking the small model first.
COMPLEX = re.compile(
    r"\b(write|code|coding|program|script|sketch|debug|function|algorithm|compile error|error message|"
    r"explain (why|how|the difference)|step by step|compare|difference between|pros and cons|analy[sz]e|"
    r"summari[sz]e|essay|email|letter|report|cover letter|resume|plan (a|my|the)|itinerary|design|calculate|"
    r"solve|prove|derive|translate|strategy|optimi[sz]e|review my|in detail|detailed|research)\b")


def is_complex(text: str) -> bool:
    return bool(COMPLEX.search(text.lower())) or len(text.split()) > 22


class LocalBrain:
    def __init__(self, cfg, registry: ToolRegistry):
        self.cfg = cfg
        self.registry = registry
        self.memory = get_store()
        self.lock = threading.Lock()
        self.visual: Callable[[str, str], None] | None = None
        self.url = cfg.get("local.url").rstrip("/") + "/api/chat"
        self.model = cfg.get("local.model")
        self.system = SYSTEM.format(user=os.environ.get("BATSY_USER_NAME") or getpass.getuser())
        self.tools = [
            {"type": "function", "function": {"name": name, "description": desc,
                                              "parameters": {"type": "object", "properties": props, "required": req}}}
            for name, (desc, props, req) in LOCAL_TOOLS.items() if name in registry.tools
        ]
        threading.Thread(target=self._warm_up, daemon=True, name="warm-up").start()

    # --- Ollama -------------------------------------------------------------
    def _options(self) -> dict:
        return {"num_ctx": self.cfg.get("local.context"), "temperature": 0.6, "num_predict": 400}

    def _warm_up(self) -> None:
        """Load the model and pre-read the system prompt so the first real reply is quick."""
        body = {"model": self.model, "stream": False, "keep_alive": self.cfg.get("local.keep_alive"),
                "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": "hi"}],
                "tools": self.tools, "options": {**self._options(), "num_predict": 1}}
        try:
            self._post(body).read()
            log.info("local model %s ready", self.model)
        except Exception as e:
            log.warning("local model warm-up failed: %s", e)

    def _post(self, body: dict):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=300)

    def _stream(self, messages: list[dict], chunker: SpeechChunker, abort: threading.Event):
        body = {"model": self.model, "stream": True, "keep_alive": self.cfg.get("local.keep_alive"),
                "messages": messages, "tools": self.tools, "options": self._options()}
        content, calls, stats = "", [], {}
        with self._post(body) as resp:
            for line in resp:
                if abort.is_set():
                    return None
                if not line.strip():
                    continue
                part = json.loads(line)
                msg = part.get("message", {})
                if msg.get("content"):
                    content += msg["content"]
                    chunker.feed(msg["content"])
                calls += msg.get("tool_calls") or []
                if part.get("done"):
                    stats = part
        self._log_usage(stats)
        return content, calls

    def _log_usage(self, s: dict) -> None:
        from batsy.usage import USAGE_FILE

        row = {"t": int(time.time()), "model": f"local:{self.model}", "in": s.get("prompt_eval_count", 0),
               "out": s.get("eval_count", 0), "cache_read": 0, "cache_write": 0}
        log.info("local usage %s (%.1fs)", row, s.get("total_duration", 0) / 1e9)
        with open(USAGE_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")

    # --- the turn -----------------------------------------------------------
    def note_announcement(self, text: str) -> None:
        self.memory.add_exchange("alert", "", text, [])

    def ask(self, text: str, source: str, say, abort: threading.Event, on_tool=None) -> str:
        with self.lock:
            return self._ask(text, source, say, abort, on_tool)

    def _ask(self, text, source, say, abort, on_tool) -> str:
        m = self.cfg.get("local")
        context = self.memory.build_context(text, self.cfg, recent=m["recent_exchanges"], related=m["related_exchanges"])
        stamp = time.strftime("%a %d %b %Y %H:%M")
        prompt = (f"{context}\n\n" if context else "") + f"[Now {stamp}] {text}"
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": prompt}]
        spoken, actions = [], []

        def emit(sentence: str) -> None:
            spoken.append(sentence)
            say(sentence)

        def on_marker(kind: str, arg: str) -> None:
            actions.append(f"showed {kind}: {arg[:60]}")
            if self.visual and not abort.is_set():
                threading.Thread(target=self.visual, args=(kind, arg), daemon=True).start()

        if self.claude_ok and is_complex(text):
            self._claude(text, context, emit, on_marker, on_tool, actions)
            return self._finish(source, text, spoken, actions, abort)

        for _ in range(m["max_steps"]):
            chunker = SpeechChunker(emit, on_marker)
            try:
                result = self._stream(messages, chunker, abort)
            except urllib.error.URLError as e:
                emit("My local brain isn't running. Start it with: systemctl start ollama.")
                log.error("ollama: %s", e)
                break
            if result is None:
                break
            chunker.flush()
            content, calls = result
            messages.append({"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})})
            if not calls:
                break
            handed_off = False
            for call in calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if name == "ask_claude" and self.claude_ok:
                    self._claude(args.get("request") or text, context, emit, on_marker, on_tool, actions)
                    handed_off = True
                    break
                if on_tool:
                    on_tool(name)
                out, is_error = self.registry.execute(name, args) if name in LOCAL_TOOLS else (f"No tool {name}.", True)
                out = out if isinstance(out, str) else "done"
                actions.append(f"{name} {json.dumps(args)[:80]} -> {'ERROR ' if is_error else ''}{out.strip()[:60]}")
                # every character here must be re-read by a slow CPU: keep it short
                messages.append({"role": "tool", "tool_name": name, "content": truncate(out, m["tool_result_chars"])})
            if abort.is_set() or handed_off:
                break
        return self._finish(source, text, spoken, actions, abort)

    def _finish(self, source, text, spoken, actions, abort) -> str:
        reply = " ".join(spoken)
        self.memory.add_exchange(source, text, reply + (" (interrupted)" if abort.is_set() else ""), actions)
        return reply

    def _claude(self, request, context, emit, on_marker, on_tool, actions) -> None:
        """Answer with Claude Code on the user's Pro plan; its reply goes straight to speech and screen."""
        if on_tool:
            on_tool("ask_claude")
        log.info("routing to Claude: %s", request[:120])
        answer = ask_claude(request, context)
        chunker = SpeechChunker(emit, on_marker)
        chunker.feed(answer)
        chunker.flush()
        actions.append("answered by Claude")

    def reset(self) -> None:
        with self.lock:
            self.memory.new_session()


CLAUDE_BRIEF = """You are the expert brain behind Batsy, a voice assistant on the user's Fedora laptop. Your reply is \
spoken aloud: two to four natural sentences, no markdown or lists in the spoken part. Be precise and correct.
To put something on their screen, add markers (not spoken): [[show: exact name]] for the best picture of a thing, \
[[pull: exact name]] for a person/place/thing with its facts, [[card: Title | line; line]] for steps, lists, \
numbers or code summaries. Put any code or files under {workspace} and just say where.
{context}
User: {request}"""


def ask_claude(prompt: str, context: str = "", timeout: int = 240) -> str:
    """One request to Claude Code (the official CLI, on the user's Pro plan)."""
    exe = shutil.which("claude")
    if not exe:
        return "Claude Code isn't installed. Run: npm install -g @anthropic-ai/claude-code, then claude to log in."
    full = CLAUDE_BRIEF.format(workspace=WORKSPACE, context=context, request=prompt)
    try:
        p = subprocess.run([exe, "-p", full, "--output-format", "text", "--permission-mode", "acceptEdits",
                            "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash(arduino-cli:*)"],
                           cwd=WORKSPACE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "Claude took too long to answer."
    return (p.stdout.strip() or p.stderr.strip() or "Claude didn't answer.")[:1500]
