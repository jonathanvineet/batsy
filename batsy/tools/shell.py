"""Shell access: foreground commands, background jobs, and self-restart."""
import itertools
import os
import signal
import subprocess
import threading
import time

from batsy import safety
from batsy.config import HOME, JOBS_DIR
from batsy.tools import truncate

_jobs: dict[str, dict] = {}
_job_ids = itertools.count(1)


def _shell_env() -> dict:
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    return env


def _watch_job(ctx, job_id: str) -> None:
    job = _jobs[job_id]
    code = job["proc"].wait()
    job["exit_code"] = code
    job["ended"] = time.time()
    if job["notify"]:
        tail = job["log"].read_text(errors="replace")[-400:].strip().splitlines()
        last = tail[-1] if tail else ""
        status = "finished" if code == 0 else f"failed with exit code {code}"
        ctx.announce(f"Background job {job['label']} {status}. {last[:160]}")


def register(reg):
    @reg.tool(
        "run_shell",
        "Run a bash command on the user's Fedora laptop as the user (login shell, full environment). "
        "Returns exit code, stdout and stderr. Use background=true for anything long-running, "
        "anything that opens a GUI app, or servers; background jobs announce when they finish. "
        "For root use `pkexec <cmd>` (shows the GNOME password dialog) - never plain sudo, there is no terminal to type a password.",
        {
            "command": {"type": "string", "description": "bash command line"},
            "cwd": {"type": "string", "description": "working directory, default home"},
            "timeout": {"type": "integer", "description": "seconds before a foreground command is killed (default 120)"},
            "background": {"type": "boolean", "description": "start detached and return a job id immediately"},
            "label": {"type": "string", "description": "short spoken name for a background job"},
            "notify_when_done": {"type": "boolean", "description": "announce when a background job ends (default true)"},
        },
        ["command"],
    )
    def run_shell(ctx, command, cwd=None, timeout=120, background=False, label=None, notify_when_done=True):
        reason = safety.danger_reason(command)
        if reason and ctx.cfg.get("safety.confirm_dangerous"):
            if not ctx.confirm(f"This command will {reason}: {command[:120]}. Should I go ahead?"):
                return "The user declined. Command NOT run."
        cwd = os.path.expanduser(cwd) if cwd else str(HOME)

        if background:
            job_id = str(next(_job_ids))
            log = JOBS_DIR / f"job-{job_id}.log"
            fh = open(log, "wb")
            proc = subprocess.Popen(
                ["bash", "-lc", command], cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, env=_shell_env(),
            )
            _jobs[job_id] = {
                "proc": proc, "command": command, "log": log, "label": label or f"number {job_id}",
                "started": time.time(), "exit_code": None, "notify": notify_when_done,
            }
            threading.Thread(target=_watch_job, args=(ctx, job_id), daemon=True).start()
            return f"Started background job {job_id} (pid {proc.pid}). Log: {log}"

        try:
            p = subprocess.run(
                ["bash", "-lc", command], cwd=cwd, capture_output=True, text=True,
                timeout=timeout, stdin=subprocess.DEVNULL, env=_shell_env(), errors="replace",
            )
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            return f"TIMED OUT after {timeout}s. Partial output:\n{truncate(out, 6000)}\nRe-run with background=true if it needs longer."
        parts = [f"exit code: {p.returncode}"]
        if p.stdout.strip():
            parts.append(f"stdout:\n{truncate(p.stdout, 12000)}")
        if p.stderr.strip():
            parts.append(f"stderr:\n{truncate(p.stderr, 4000)}")
        return "\n".join(parts)

    @reg.tool(
        "jobs",
        "Manage background jobs started with run_shell: list them, read a job's output, or stop one.",
        {
            "action": {"type": "string", "enum": ["list", "output", "stop"]},
            "job_id": {"type": "string"},
            "tail_chars": {"type": "integer", "description": "how much of the log end to return (default 4000)"},
        },
        ["action"],
    )
    def jobs(ctx, action, job_id=None, tail_chars=4000):
        if action == "list":
            if not _jobs:
                return "No background jobs."
            rows = []
            for jid, j in _jobs.items():
                state = "running" if j["exit_code"] is None else f"exited {j['exit_code']}"
                rows.append(f"[{jid}] {state} {int(time.time() - j['started'])}s ago: {j['command'][:100]}")
            return "\n".join(rows)
        job = _jobs.get(job_id or "")
        if not job:
            return f"No job with id {job_id}."
        if action == "output":
            return job["log"].read_text(errors="replace")[-tail_chars:] or "(no output yet)"
        if job["exit_code"] is not None:
            return "Job already finished."
        job["notify"] = False
        os.killpg(job["proc"].pid, signal.SIGTERM)
        return f"Stopped job {job_id}."

    @reg.tool(
        "restart_self",
        "Restart Batsy after this reply (e.g. after editing Batsy's own source code or config). "
        "Batsy's code is byte-compiled first; if it does not compile the restart is refused.",
        {},
    )
    def restart_self(ctx):
        from batsy.config import SOURCE_DIR
        import sys

        check = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(SOURCE_DIR)], capture_output=True, text=True
        )
        if check.returncode != 0:
            return f"Refusing to restart, the code has errors:\n{check.stdout}{check.stderr}"
        ctx.flags["restart"] = True
        return "Restart scheduled for right after this reply."
