"""Tool registry: every capability Claude can call.

Each tool module exposes `register(registry)` and adds tools with `registry.tool(...)`.
"""
import json
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_RESULT_CHARS = 16000

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


@dataclass
class ToolContext:
    """What tools may reach back into: config, the voice, and the user."""
    cfg: Any
    announce: Callable[[str], None]          # speak + desktop-notify, from any thread
    confirm: Callable[[str], bool]           # ask the user yes/no, blocks
    abort: threading.Event = field(default_factory=threading.Event)
    flags: dict = field(default_factory=dict)  # e.g. {"restart": True, "new_conversation": True}


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., Any]


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} chars cut] ...\n\n{text[-half:]}"


class ToolRegistry:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.tools: dict[str, Tool] = {}

    def tool(self, name: str, description: str, properties: dict, required: list[str] | None = None):
        def deco(fn):
            schema = {"type": "object", "properties": properties, "required": required or []}
            self.tools[name] = Tool(name, description, schema, fn)
            return fn
        return deco

    def definitions(self) -> list[dict]:
        # Stable order keeps the prompt cache warm.
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.schema,
                "eager_input_streaming": True,
            }
            for t in self.tools.values()
        ]

    def _validate(self, tool: Tool, args: Any) -> str | None:
        # Eager input streaming hands us unvalidated JSON, so check it ourselves.
        if not isinstance(args, dict):
            return "input must be a JSON object"
        for key in tool.schema["required"]:
            if key not in args:
                return f"missing required field '{key}'"
        for key, value in args.items():
            spec = tool.schema["properties"].get(key)
            if spec is None:
                return f"unknown field '{key}'"
            check = _TYPE_CHECKS.get(spec.get("type"))
            if check and value is not None and not check(value):
                return f"field '{key}' must be {spec['type']}"
            if "enum" in spec and value not in spec["enum"]:
                return f"field '{key}' must be one of {spec['enum']}"
        return None

    def execute(self, name: str, args: Any) -> tuple[Any, bool]:
        """Run a tool. Returns (content, is_error). Content is a str or a list of content blocks."""
        tool = self.tools.get(name)
        if tool is None:
            return f"Unknown tool '{name}'.", True
        problem = self._validate(tool, args)
        if problem:
            return f"INVALID_INPUT: {problem}. Re-issue the call with valid input.", True
        try:
            result = tool.handler(self.ctx, **args)
        except Exception as e:  # tools must never crash the assistant
            return f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}", True
        if isinstance(result, list):
            return result, False
        if not isinstance(result, str):
            result = json.dumps(result, indent=1, default=str)
        return truncate(result), False


def build_registry(ctx: ToolContext) -> ToolRegistry:
    from batsy.tools import arduino, files, memory, monitor, screen, shell, skills, system

    reg = ToolRegistry(ctx)
    for mod in (shell, files, system, screen, arduino, monitor, memory, skills):
        mod.register(reg)
    return reg
