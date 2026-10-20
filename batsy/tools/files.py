"""Reading and writing files directly (no shell quoting headaches when writing code)."""
import os
from pathlib import Path

from batsy.config import HOME
from batsy.tools import truncate


def _resolve(path: str) -> Path:
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else HOME / p


def register(reg):
    @reg.tool(
        "read_file",
        "Read a text file. Relative paths are from the home directory.",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based first line (optional)"},
            "max_lines": {"type": "integer", "description": "default 200"},
        },
        ["path"],
    )
    def read_file(ctx, path, start_line=1, max_lines=200):
        p = _resolve(path)
        lines = p.read_text(errors="replace").splitlines()
        start = max(start_line, 1) - 1
        chunk = lines[start:start + max_lines]
        body = "\n".join(f"{i + start + 1:5} {line}" for i, line in enumerate(chunk))
        more = f"\n[{len(lines) - start - len(chunk)} more lines]" if start + len(chunk) < len(lines) else ""
        return truncate(f"{p} ({len(lines)} lines)\n{body}{more}")

    @reg.tool(
        "write_file",
        "Create or overwrite a file with the given content (parent folders are created). "
        "Use this for code, sketches, configs and scripts. Set append=true to add to the end instead.",
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean"},
            "executable": {"type": "boolean", "description": "chmod +x after writing"},
        },
        ["path", "content"],
    )
    def write_file(ctx, path, content, append=False, executable=False):
        p = _resolve(path)
        if not str(p).startswith(str(HOME)) and ctx.cfg.get("safety.confirm_dangerous"):
            if not ctx.confirm(f"I want to write to {p}, outside your home folder. Okay?"):
                return "The user declined. Nothing written."
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w") as f:
            f.write(content)
        if executable:
            p.chmod(p.stat().st_mode | 0o111)
        return f"{'Appended' if append else 'Wrote'} {len(content)} chars to {p}"

    @reg.tool(
        "edit_file",
        "Replace one exact occurrence of old_text with new_text in a file. old_text must match exactly once.",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        ["path", "old_text", "new_text"],
    )
    def edit_file(ctx, path, old_text, new_text):
        p = _resolve(path)
        text = p.read_text()
        count = text.count(old_text)
        if count != 1:
            return f"old_text found {count} times in {p}; it must match exactly once. Read the file and retry."
        p.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {p}"

    @reg.tool(
        "list_dir",
        "List a directory (names, sizes, type). Use run_shell with find/grep for searches.",
        {"path": {"type": "string"}, "show_hidden": {"type": "boolean"}},
        ["path"],
    )
    def list_dir(ctx, path, show_hidden=False):
        p = _resolve(path)
        rows = []
        for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith(".") and not show_hidden:
                continue
            if entry.is_dir():
                rows.append(f"{entry.name}/")
            else:
                rows.append(f"{entry.name}  {entry.stat().st_size} B")
        return truncate(f"{p}:\n" + "\n".join(rows[:500]) + (f"\n... {len(rows) - 500} more" if len(rows) > 500 else ""))
