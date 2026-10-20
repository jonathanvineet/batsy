"""Self-written skills: Python code Batsy writes for itself, saved and reusable across sessions.

A skill is ~/.local/share/batsy/skills/<name>.py with a module docstring (its description),
an optional PARAMETERS dict documenting arguments, and `def run(**kwargs) -> str`.
Skills run in a separate process so a buggy skill can never take Batsy down.
"""
import ast
import json
import re
import subprocess
import sys

from batsy.config import SKILLS_DIR
from batsy.tools import truncate

_RUNNER = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("skill", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
result = mod.run(**json.loads(sys.argv[2]))
print("\n__BATSY_RESULT__")
print(result if isinstance(result, str) else json.dumps(result, default=str))
"""

TEMPLATE_HINT = '''"""One-line description of what the skill does."""
PARAMETERS = {"city": "name of the city"}  # documents the kwargs

def run(city: str = "Chennai") -> str:
    ...
    return "text result"
'''


def _describe(path) -> str:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return "(syntax error)"
    doc = ast.get_docstring(tree) or "(no description)"
    params = ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PARAMETERS" for t in node.targets):
            try:
                params = f" params={ast.literal_eval(node.value)}"
            except ValueError:
                pass
    return f"{doc.splitlines()[0]}{params}"


def list_skills() -> str:
    files = sorted(SKILLS_DIR.glob("*.py"))
    return "\n".join(f"- {f.stem}: {_describe(f)}" for f in files) or "(no skills yet)"


def register(reg):
    @reg.tool(
        "create_skill",
        "Teach yourself a new reusable ability by writing Python code. Use when the user asks you to learn/automate "
        "something they'll want again (e.g. 'make a skill that checks my college portal'). Code must define "
        f"`run(**kwargs) -> str`, a module docstring, and optionally PARAMETERS. Example:\n{TEMPLATE_HINT}"
        f"It runs with Batsy's own Python ({sys.executable}); install packages with "
        f"`{sys.executable} -m pip install ...` via run_shell. Overwrites an existing skill of the same name. "
        "Test it with run_skill right after creating it.",
        {
            "name": {"type": "string", "description": "snake_case name"},
            "code": {"type": "string"},
        },
        ["name", "code"],
    )
    def create_skill(ctx, name, code):
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
            return "Name must be snake_case letters/digits/underscores."
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"Syntax error, not saved: {e}"
        if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body):
            return "The code must define a top-level `run(**kwargs)` function. Not saved."
        path = SKILLS_DIR / f"{name}.py"
        existed = path.exists()
        path.write_text(code)
        return f"{'Updated' if existed else 'Created'} skill '{name}' at {path}."

    @reg.tool(
        "run_skill",
        "Run one of your saved skills. Current skills are listed by list_skills.",
        {
            "name": {"type": "string"},
            "args": {"type": "object", "description": "keyword arguments for run()"},
            "timeout": {"type": "integer", "description": "seconds, default 300"},
        },
        ["name"],
    )
    def run_skill(ctx, name, args=None, timeout=300):
        path = SKILLS_DIR / f"{name}.py"
        if not path.exists():
            return f"No skill named '{name}'. Skills:\n{list_skills()}"
        p = subprocess.run([sys.executable, "-c", _RUNNER, str(path), json.dumps(args or {})],
                           capture_output=True, text=True, timeout=timeout, errors="replace")
        out, _, result = p.stdout.partition("\n__BATSY_RESULT__\n")
        if p.returncode != 0:
            return f"Skill crashed (exit {p.returncode}):\n{truncate(p.stderr, 6000)}\nstdout:\n{truncate(out, 2000)}"
        text = f"result:\n{result.strip()}"
        if out.strip():
            text += f"\nprinted:\n{truncate(out, 3000)}"
        return truncate(text)

    @reg.tool("list_skills", "List the skills you have written for yourself.", {})
    def _list_skills(ctx):
        return list_skills()

    @reg.tool(
        "delete_skill",
        "Delete a saved skill.",
        {"name": {"type": "string"}},
        ["name"],
    )
    def delete_skill(ctx, name):
        path = SKILLS_DIR / f"{name}.py"
        if not path.exists():
            return f"No skill '{name}'."
        path.unlink()
        return f"Deleted skill '{name}'."
