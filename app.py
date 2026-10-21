#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi>=0.110", "uvicorn>=0.27", "pydantic>=2.0"]
# ///
"""Panorama - every project you have open, in one field of view.

Reads three things and joins them:
  1. git worktrees under PANORAMA_ROOTS (what is checked out where)
  2. Claude Code session logs in ~/.claude/projects (what the agents are doing)
  3. a local SQLite file (your notes and todos per worktree)

Nothing is written outside the SQLite file. Git is only ever read from.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

__version__ = "0.2.1"

HOME = Path.home()
# Bundled files (static/) sit next to app.py, or in the unpacked bundle when
# running as a PyInstaller one-file binary.
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

# Every option panorama.env understands. The startup code, the settings page
# and its API all read this one table. "live" options apply on the next poll
# after a change; the others need a restart.
OPTIONS: list[dict[str, Any]] = [
    {"name": "PANORAMA_ROOTS", "label": "Project folders", "default": "~/work", "live": True,
     "kind": "paths", "help": "Folders to scan for git repos, separated by colons."},
    {"name": "PANORAMA_REPOS", "label": "Extra repos", "default": "", "live": True,
     "kind": "paths", "help": "Repos outside those folders, listed one by one, separated by colons."},
    {"name": "PANORAMA_SCAN_DEPTH", "label": "Scan depth", "default": "3", "live": True,
     "kind": "int", "min": 1, "max": 10, "help": "How many levels deep to look for repos under each folder."},
    {"name": "PANORAMA_CLAUDE_DIR", "label": "Claude Code logs", "default": "~/.claude/projects", "live": True,
     "kind": "path", "help": "Where Claude Code keeps its session logs."},
    {"name": "PANORAMA_JIRA_BASE", "label": "Jira base URL", "default": "", "live": True,
     "kind": "url", "help": "Makes ABC-123 in a branch name a link. Leave empty to disable."},
    {"name": "PANORAMA_CACHE_SECONDS", "label": "Refresh floor (seconds)", "default": "3", "live": True,
     "kind": "number", "min": 1, "max": 60, "help": "Git is asked at most this often."},
    {"name": "PANORAMA_DB", "label": "Notes database", "default": "~/.local/share/panorama/panorama.db",
     "live": False, "kind": "file", "help": "SQLite file with your notes and todos."},
    {"name": "PORT", "label": "Port", "default": "8787", "live": False,
     "kind": "int", "min": 1024, "max": 65535, "help": "The board is served on 127.0.0.1 at this port."},
]
OPTION_BY_NAME = {o["name"]: o for o in OPTIONS}

# Options set as real environment variables win over the file, so the settings
# page shows them read-only. Recorded before the file is read.
FROM_ENVIRONMENT = {name for name in OPTION_BY_NAME if name in os.environ}
# Set by Panorama.app, which starts its own server on a free port each launch.
STARTED_BY_APP = os.environ.get("PANORAMA_APP") == "1"
FILE_VALUES: dict[str, str] = {}


def parse_env_lines(text: str) -> dict[str, str]:
    """NAME=value pairs from panorama.env text; the first occurrence wins."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return values


def load_env_file() -> Path | None:
    """Read the first panorama.env found and return its path, or None.

    Lookup order: ./panorama.env (current directory), then
    ~/.config/panorama/panorama.env. $PANORAMA_ENV, when set, replaces that
    search: only that file is read (a missing one means no settings yet), and
    the Settings page saves to it. Real environment variables always win.
    """
    if os.environ.get("PANORAMA_ENV"):
        candidates = [Path(os.environ["PANORAMA_ENV"]).expanduser()]
    else:
        candidates = [Path.cwd() / "panorama.env", HOME / ".config" / "panorama" / "panorama.env"]
    for path in candidates:
        if not path or not path.is_file():
            continue
        # Only known options: the file must not be a way to set arbitrary
        # environment variables (GIT_CONFIG_*, PATH, ...) for the git calls.
        values = parse_env_lines(path.read_text(errors="replace"))
        FILE_VALUES.update({k: v for k, v in values.items() if k in OPTION_BY_NAME})
        for key, val in FILE_VALUES.items():
            os.environ.setdefault(key, val)
        return path
    return None


ENV_FILE = load_env_file()


def option(name: str) -> str:
    """Configured value: environment, then file, then default."""
    return os.environ.get(name, OPTION_BY_NAME[name]["default"])


def split_paths(value: str) -> list[Path]:
    return [Path(p.strip()).expanduser() for p in value.split(":") if p.strip()]


def validate_option(name: str, value: str) -> str | None:
    """An error message for this value, or None when it is acceptable."""
    meta = OPTION_BY_NAME[name]
    if not value.isprintable():
        return "Must be a single line of plain text."
    if value and (value[0] in "\"'" or value[-1] in "\"'"):
        return "Leave out the quotes."
    kind = meta["kind"]
    if kind in ("int", "number"):
        try:
            number = int(value) if kind == "int" else float(value)
        except ValueError:
            return "Must be a whole number." if kind == "int" else "Must be a number."
        if not meta["min"] <= number <= meta["max"]:
            return f"Must be between {meta['min']} and {meta['max']}."
    elif kind == "paths":
        if name == "PANORAMA_ROOTS" and not split_paths(value):
            return "Give at least one folder."
    elif kind == "path":
        if not value:
            return "Give a folder."
    elif kind == "file":
        if not value:
            return "Give a file path."
        if Path(value).expanduser().is_dir():
            return "This is a folder; give a file path."
    elif kind == "url":
        if value and not value.startswith(("http://", "https://")):
            return "Must start with http:// or https://, or be empty."
    return None


# Options whose value (from panorama.env or the environment) is not valid:
# name -> message. They fall back to their default instead of stopping startup.
INVALID_OPTIONS: dict[str, str] = {}
INVALID_VALUES: dict[str, str] = {}  # the rejected raw value, shown in its field


def drop_invalid_options() -> None:
    """Replace invalid option values with their defaults, remembering why."""
    for name in OPTION_BY_NAME:
        if name not in os.environ:
            continue
        value = os.environ[name].strip()
        error = validate_option(name, value)
        if name == "PANORAMA_ROOTS" and not value and os.environ.get("PANORAMA_REPOS", "").strip():
            error = None  # no roots, only the listed repos: a valid setup
        if error:
            where = "the environment" if name in FROM_ENVIRONMENT else "panorama.env"
            INVALID_OPTIONS[name] = (f"{name}={value} in {where} is not valid ({error.rstrip('.')}); "
                                     f"using the default, {OPTION_BY_NAME[name]['default'] or 'empty'}.")
            INVALID_VALUES[name] = value
            del os.environ[name]


def apply_live_options() -> None:
    """(Re)read the options that may change while running."""
    global ROOTS, EXTRA_REPOS, CLAUDE_PROJECTS, JIRA_BASE, SCAN_DEPTH, CACHE_SECONDS
    ROOTS = split_paths(option("PANORAMA_ROOTS"))
    # Repos that live nowhere near your roots. Listed one by one, not scanned.
    EXTRA_REPOS = split_paths(option("PANORAMA_REPOS"))
    CLAUDE_PROJECTS = Path(option("PANORAMA_CLAUDE_DIR")).expanduser()
    JIRA_BASE = option("PANORAMA_JIRA_BASE").rstrip("/")
    SCAN_DEPTH = int(option("PANORAMA_SCAN_DEPTH"))
    CACHE_SECONDS = float(option("PANORAMA_CACHE_SECONDS"))


drop_invalid_options()
ROOTS: list[Path] = []
EXTRA_REPOS: list[Path] = []
CLAUDE_PROJECTS = Path()
JIRA_BASE = ""
SCAN_DEPTH = 3
CACHE_SECONDS = 3.0
apply_live_options()
DB_PATH = Path(option("PANORAMA_DB")).expanduser()
# What the running process started with, to tell when a saved change still
# needs a restart.
STARTED_WITH = {name: option(name) for name in OPTION_BY_NAME if not OPTION_BY_NAME[name]["live"]}

TICKET_RE = re.compile(r"([A-Z][A-Z0-9]{1,9}-\d+)")
SKIP_DIRS = {
    "node_modules", "target", "build", "dist", ".gradle", ".idea", ".m2",
    "venv", ".venv", "__pycache__", ".mvn", "out", "vendor",
}

app = FastAPI(title="Panorama", version=__version__)
# Only answer requests addressed to this machine. Stops a web page from using
# DNS rebinding to read the board or change its configuration.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def refuse_cross_site_writes(request: Request, call_next: Any) -> Any:
    """Writes only from Panorama's own page, never from another website.

    Browsers label every write with where it came from (Sec-Fetch-Site,
    Origin). Anything not from 127.0.0.1/localhost is refused, and a write
    with a body must be JSON, which no plain HTML form can send. Requests
    without these headers (curl, scripts on this machine) pass: they could
    edit the files directly anyway.
    """
    if request.method in WRITE_METHODS:
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        # exact match with the address this request was sent to (TrustedHost
        # already limits that to 127.0.0.1/localhost), so another local app on
        # a different port is refused too
        own = f"http://{request.headers.get('host', '')}"
        if site in ("cross-site", "same-site") or (origin is not None and origin != own):
            return JSONResponse({"detail": "Writes from other websites are refused."}, status_code=403)
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if request.method != "DELETE" and content_type != "application/json":
            return JSONResponse({"detail": "Send the request body as JSON."}, status_code=415)
    return await call_next(request)
# Serves the manifest and icons for "Add to Dock" / "Install app". index.html
# itself is returned by the "/" route below.
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def canonical(path: str) -> str:
    """Resolve symlinks, so /tmp/x and /private/tmp/x compare equal on macOS."""
    return os.path.realpath(path) if path else ""


def is_within(child: str, parent: str) -> bool:
    """True if child is parent or below it. For comparison only: keys stored
    in SQLite stay unnormalised."""
    if not child or not parent:
        return False
    c = os.path.normpath(child)
    p = os.path.normpath(parent)
    return c == p or c.startswith(p.rstrip("/") + "/")


def best_lane(path: str, lane_paths: list[str]) -> str | None:
    """The deepest lane path that contains path, or None.

    Worktrees can nest (Claude Code puts its own under <repo>/.claude/worktrees),
    so a session inside a nested worktree is also inside the main checkout; it
    belongs only to the nested one.
    """
    matches = [lane for lane in lane_paths if is_within(path, lane)]
    return max(matches, key=len) if matches else None


def short_path(path: str, home: str = str(HOME)) -> str:
    """Display form with the home directory shown as ~."""
    if not is_within(path, home):
        return path
    return "~" + os.path.normpath(path)[len(os.path.normpath(home).rstrip("/")):]


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git(path: str | Path, *args: str, timeout: float = 8) -> str:
    """Run a read-only git command. Returns '' on any failure."""
    try:
        proc = subprocess.run(
            # core.fsmonitor can name a program to run; never honour it from a
            # repo Panorama merely reads.
            ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def find_repos() -> dict[str, str]:
    """Map git-common-dir -> one working directory of that repo."""
    repos: dict[str, str] = {}

    def register(dirpath: str) -> None:
        common = git(dirpath, "rev-parse", "--git-common-dir")
        if not common:
            return
        resolved = Path(common)
        if not resolved.is_absolute():
            resolved = (Path(dirpath) / common).resolve()
        repos.setdefault(str(resolved), dirpath)

    for root in ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel = Path(dirpath).relative_to(root).parts
            if len(rel) >= SCAN_DEPTH:
                dirnames[:] = []
            has_git = ".git" in dirnames or ".git" in filenames
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
            if has_git:
                register(dirpath)
                # every worktree of this repo is reachable via `git worktree list`
                dirnames[:] = []

    for repo in EXTRA_REPOS:
        if repo.is_dir():
            register(str(repo))

    return repos


def list_worktrees(repo_dir: str) -> list[dict[str, Any]]:
    out = git(repo_dir, "worktree", "list", "--porcelain")
    trees: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                trees.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": val}
        elif key == "branch":
            cur["branch"] = val.replace("refs/heads/", "")
        elif key == "HEAD":
            cur["head"] = val[:8]
        elif key == "detached":
            cur["branch"] = None
        elif key == "bare":
            cur["bare"] = True
    if cur:
        trees.append(cur)
    return [t for t in trees if not t.get("bare") and Path(t["path"]).is_dir()]


def base_ref(path: str) -> str:
    ref = git(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if ref:
        return ref.replace("refs/remotes/", "")
    for candidate in ("origin/main", "origin/master", "main", "master", "develop"):
        if git(path, "rev-parse", "--verify", "--quiet", candidate):
            return candidate
    return ""


def parse_shortstat(text: str) -> dict[str, int]:
    files = ins = dels = 0
    m = re.search(r"(\d+) files? changed", text)
    if m:
        files = int(m.group(1))
    m = re.search(r"(\d+) insertions?", text)
    if m:
        ins = int(m.group(1))
    m = re.search(r"(\d+) deletions?", text)
    if m:
        dels = int(m.group(1))
    return {"files": files, "insertions": ins, "deletions": dels}


def worktree_facts(path: str) -> dict[str, Any]:
    status_lines = [l for l in git(path, "status", "--porcelain").splitlines() if l.strip()]
    untracked = sum(1 for l in status_lines if l.startswith("??"))

    ahead = behind = None
    tracking = git(path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if tracking:
        parts = re.split(r"\s+", tracking)
        if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]):
            behind, ahead = int(parts[0]), int(parts[1])

    base = base_ref(path)
    committed = parse_shortstat(git(path, "diff", "--shortstat", f"{base}...HEAD")) if base else {}
    working = parse_shortstat(git(path, "diff", "--shortstat", "HEAD"))

    last = git(path, "log", "-1", "--format=%h\x1f%ar\x1f%s")
    commit = {}
    if last and "\x1f" in last:
        sha, ago, subject = (last.split("\x1f") + ["", ""])[:3]
        commit = {"sha": sha, "ago": ago, "subject": subject}

    return {
        "dirty_files": len(status_lines),
        "untracked": untracked,
        "ahead": ahead,
        "behind": behind,
        "base": base,
        "committed": committed,
        "working": working,
        "last_commit": commit,
    }


# --------------------------------------------------------------------------
# Claude Code sessions
# --------------------------------------------------------------------------

_head_cache: dict[str, dict[str, Any]] = {}


def block_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return " ".join(parts).strip()
    return ""


def session_head(fp: Path) -> dict[str, Any]:
    """First user prompt + cwd. Immutable once written, so cache it forever."""
    cached = _head_cache.get(str(fp))
    if cached:
        return cached
    cwd = title = started = None
    try:
        with fp.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 60 or (cwd and title):
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = cwd or rec.get("cwd")
                started = started or rec.get("timestamp")
                if title is None and rec.get("type") == "user":
                    text = block_text((rec.get("message") or {}).get("content"))
                    # skip system-injected turns and tool results
                    if text and not text.startswith("<") and not text.startswith("Caveat:"):
                        title = " ".join(text.split())[:140]
    except OSError:
        pass
    head = {"cwd": cwd, "title": title, "started": started}
    if cwd:
        _head_cache[str(fp)] = head
    return head


def session_tail(fp: Path, size: int) -> dict[str, Any]:
    """Last parseable record, read from the end of the file."""
    try:
        with fp.open("rb") as fh:
            fh.seek(max(0, size - 250_000))
            chunk = fh.read()
    except OSError:
        return {}
    for raw in reversed([l for l in chunk.split(b"\n") if l.strip()]):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return {}


def classify(last: dict[str, Any], age: float) -> tuple[str, str]:
    """(status, hint). Tune the thresholds to how you actually work."""
    content = (last.get("message") or {}).get("content")
    pending_tool = isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for b in content
    )
    if age < 45:
        return "running", "active in the last minute"
    if age < 900:
        if pending_tool:
            return "needs-you", "stopped mid-tool - probably a permission prompt"
        return "needs-you", "turn finished, waiting on your reply"
    if age < 86_400:
        return "idle", "quiet for a while"
    return "stale", "untouched for over a day"


def collect_sessions() -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    if not CLAUDE_PROJECTS.is_dir():
        return sessions
    now = time.time()
    for fp in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            st = fp.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        age = now - st.st_mtime
        if age > 7 * 86_400:
            continue
        head = session_head(fp)
        last = session_tail(fp, st.st_size)
        status, hint = classify(last, age)
        sessions.append({
            "id": fp.stem,
            "file": str(fp),
            "cwd": head.get("cwd") or last.get("cwd"),
            "title": head.get("title") or "(no prompt recorded)",
            "status": status,
            "hint": hint,
            "age_seconds": int(age),
            "last_activity": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "size_kb": round(st.st_size / 1024),
        })
    return sessions


def running_claude_pids() -> dict[str, list[int]]:
    """Best effort: cwd -> pids of live `claude` processes. macOS/Linux."""
    found: dict[str, list[int]] = {}
    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in ps.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(" ")
        if not pid.isdigit() or "panorama" in cmd:
            continue
        first = cmd.split()[0] if cmd.split() else ""
        if Path(first).name != "claude" and "/claude " not in f"{cmd} ":
            continue
        try:
            lsof = subprocess.run(
                ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for out_line in lsof.splitlines():
            if out_line.startswith("n"):
                found.setdefault(canonical(out_line[1:]), []).append(int(pid))
    return found


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                key        TEXT PRIMARY KEY,
                body       TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS todos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key        TEXT NOT NULL,
                text       TEXT NOT NULL,
                done       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                done_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS todos_by_key ON todos(key);
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_notes() -> dict[str, str]:
    with db() as conn:
        return {r["key"]: r["body"] for r in conn.execute("SELECT key, body FROM notes")}


def load_todos() -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT id, key, text, done FROM todos ORDER BY done, id"
        ).fetchall()
    for row in rows:
        grouped.setdefault(row["key"], []).append(
            {"id": row["id"], "text": row["text"], "done": bool(row["done"])}
        )
    return grouped


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

_overview_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def build_overview() -> dict[str, Any]:
    sessions = collect_sessions()
    live = running_claude_pids()
    notes = load_notes()
    todos = load_todos()

    lanes: list[dict[str, Any]] = []
    claimed: set[str] = set()
    session_cwds = {s["id"]: canonical(s.get("cwd") or "") for s in sessions}

    for common_dir, sample in find_repos().items():
        repo_name = Path(common_dir).parent.name or Path(sample).name
        for tree in list_worktrees(sample):
            path = tree["path"]
            branch = tree.get("branch")
            ticket = None
            if branch:
                match = TICKET_RE.search(branch.upper())
                if match:
                    ticket = match.group(1)

            lanes.append({
                "key": path,
                "project_key": common_dir,
                "repo": repo_name,
                "path": path,
                "short_path": short_path(path),
                "branch": branch or f"detached @ {tree.get('head', '')}",
                "ticket": ticket,
                "ticket_url": f"{JIRA_BASE}/browse/{ticket}" if ticket and JIRA_BASE else None,
                "git": worktree_facts(path),
                "sessions": [],
                "live_pids": [],
                "note": notes.get(path, ""),
                "todos": todos.get(path, []),
                "_real_path": canonical(path),
            })

    # Each session and each live process goes to exactly one lane: the deepest
    # worktree that contains its directory.
    by_real_path: dict[str, dict[str, Any]] = {}
    for lane in lanes:  # first lane wins if two ever resolve to the same path
        by_real_path.setdefault(lane["_real_path"], lane)
    lane_paths = list(by_real_path)
    for session in sessions:
        home = best_lane(session_cwds[session["id"]], lane_paths)
        if home is not None:
            by_real_path[home]["sessions"].append(session)
            claimed.add(session["id"])
    for cwd, pids in live.items():
        home = best_lane(cwd, lane_paths)
        if home is not None:
            by_real_path[home]["live_pids"].extend(pids)
    for lane in lanes:
        lane["sessions"].sort(key=lambda s: s["age_seconds"])
        del lane["_real_path"]

    for lane in lanes:
        git_info = lane["git"]
        open_todos = sum(1 for t in lane["todos"] if not t["done"])
        score = 0
        score += 1000 * sum(1 for s in lane["sessions"] if s["status"] == "needs-you")
        score += 500 * sum(1 for s in lane["sessions"] if s["status"] == "running")
        score += 20 * min(open_todos, 10)
        score += 10 if git_info["dirty_files"] else 0
        score += 5 if (git_info["ahead"] or 0) else 0
        lane["score"] = score
        lane["open_todos"] = open_todos
        # green covers working and finished, red is reserved for "your turn"
        if any(s["status"] == "needs-you" for s in lane["sessions"]):
            lane["rail"] = "input"
        elif any(s["status"] == "running" for s in lane["sessions"]):
            lane["rail"] = "active"
        elif any(s["status"] == "idle" for s in lane["sessions"]):
            lane["rail"] = "done"
        elif git_info["dirty_files"]:
            lane["rail"] = "dirty"
        elif open_todos:
            lane["rail"] = "todo"
        else:
            lane["rail"] = "quiet"

    lanes.sort(key=lambda l: (-l["score"], l["repo"], l["branch"]))

    projects: dict[str, dict[str, Any]] = {}
    for lane in lanes:
        pkey = lane["project_key"]
        project = projects.get(pkey)
        if project is None:
            note_key = f"project:{pkey}"
            project = projects[pkey] = {
                "key": pkey,
                "note_key": note_key,
                "name": lane["repo"],
                "lanes": [],
                "note": notes.get(note_key, ""),
                "todos": todos.get(note_key, []),
            }
        project["lanes"].append(lane)

    for project in projects.values():
        sessions_here = [s for lane in project["lanes"] for s in lane["sessions"]]
        needs_you = sum(1 for s in sessions_here if s["status"] == "needs-you")
        running = sum(1 for s in sessions_here if s["status"] == "running")
        if needs_you:
            project["status"] = "input"
        elif running:
            project["status"] = "active"
        elif any(s["status"] == "idle" for s in sessions_here):
            project["status"] = "done"
        elif any(l["git"]["dirty_files"] for l in project["lanes"]):
            project["status"] = "dirty"
        else:
            project["status"] = "quiet"
        project["counts"] = {
            "worktrees": len(project["lanes"]),
            "needs_you": needs_you,
            "running": running,
            "dirty": sum(1 for l in project["lanes"] if l["git"]["dirty_files"]),
            "open_todos": sum(l["open_todos"] for l in project["lanes"])
                          + sum(1 for t in project["todos"] if not t["done"]),
        }
        project["score"] = 1000 * needs_you + 500 * running + project["counts"]["open_todos"]

    ordered = sorted(projects.values(), key=lambda p: (-p["score"], p["name"]))

    orphans = [s for s in sessions if s["id"] not in claimed]
    orphans.sort(key=lambda s: s["age_seconds"])

    return {
        "generated_at": now_iso(),
        "roots": [str(r) for r in ROOTS],
        "projects": ordered,
        "orphan_sessions": orphans[:25],
        "inbox": {"note": notes.get("inbox", ""), "todos": todos.get("inbox", [])},
        "totals": {
            "projects": len(ordered),
            "worktrees": len(lanes),
            "sessions": len(sessions),
            "needs_you": sum(1 for s in sessions if s["status"] == "needs-you"),
            "running": sum(1 for s in sessions if s["status"] == "running"),
            "dirty": sum(1 for l in lanes if l["git"]["dirty_files"]),
            "open_todos": sum(1 for group in todos.values() for t in group if not t["done"]),
        },
    }


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class NoteIn(BaseModel):
    key: str
    body: str


class TodoIn(BaseModel):
    key: str
    text: str


class TodoPatch(BaseModel):
    done: bool


class SettingIn(BaseModel):
    theme: str


@app.get("/api/overview")
def overview(fresh: bool = False) -> dict[str, Any]:
    now = time.time()
    if not fresh and _overview_cache["payload"] and now - _overview_cache["at"] < CACHE_SECONDS:
        return _overview_cache["payload"]
    payload = build_overview()
    _overview_cache.update(at=now, payload=payload)
    return payload


@app.put("/api/notes")
def save_note(note: NoteIn) -> dict[str, Any]:
    with db() as conn:
        conn.execute(
            "INSERT INTO notes(key, body, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
            (note.key, note.body, now_iso()),
        )
    _overview_cache["at"] = 0
    return {"ok": True}


@app.post("/api/todos")
def add_todo(todo: TodoIn) -> dict[str, Any]:
    text = todo.text.strip()
    if not text:
        raise HTTPException(400, "Todo text is empty.")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO todos(key, text, done, created_at) VALUES(?,?,0,?)",
            (todo.key, text, now_iso()),
        )
        new_id = cur.lastrowid
    _overview_cache["at"] = 0
    return {"id": new_id, "text": text, "done": False}


@app.patch("/api/todos/{todo_id}")
def toggle_todo(todo_id: int, patch: TodoPatch) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute(
            "UPDATE todos SET done=?, done_at=? WHERE id=?",
            (int(patch.done), now_iso() if patch.done else None, todo_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "No todo with that id.")
    _overview_cache["at"] = 0
    return {"ok": True}


@app.delete("/api/todos/{todo_id}")
def delete_todo(todo_id: int) -> dict[str, Any]:
    with db() as conn:
        conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    _overview_cache["at"] = 0
    return {"ok": True}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='theme'").fetchone()
    # None or "system" both mean "follow the OS preference"; "light"/"dark" pin it.
    return {"theme": row["value"] if row else None}


@app.put("/api/settings")
def save_settings(setting: SettingIn) -> dict[str, Any]:
    if setting.theme not in ("light", "dark", "system"):
        raise HTTPException(400, "theme must be 'light', 'dark' or 'system'.")
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('theme', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (setting.theme,),
        )
    return {"ok": True}


# --------------------------------------------------------------------------
# configuration API (the settings page)
# --------------------------------------------------------------------------

def config_file_path() -> Path:
    """The file a save writes to: the one loaded, else $PANORAMA_ENV, else the per-user default."""
    if ENV_FILE:
        return ENV_FILE
    if os.environ.get("PANORAMA_ENV"):
        return Path(os.environ["PANORAMA_ENV"]).expanduser()
    return HOME / ".config" / "panorama" / "panorama.env"


def missing_paths(name: str, value: str) -> list[str]:
    kind = OPTION_BY_NAME[name]["kind"]
    if kind == "paths":
        return [str(p) for p in split_paths(value) if not p.is_dir()]
    if kind == "path" and value and not Path(value).expanduser().is_dir():
        return [str(Path(value).expanduser())]
    return []


def write_env_file(path: Path, changes: dict[str, str]) -> None:
    """Set NAME=value lines in place, keeping comments and order.

    The first NAME= line of each changed option is replaced; options not yet
    in the file are appended. Written to a temp file and renamed, so a crash
    never leaves half a file. The first save keeps a .bak copy.
    """
    path = path.resolve()  # a symlinked panorama.env keeps its link
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(errors="replace") if path.is_file() else ""
    if text and not path.with_suffix(path.suffix + ".bak").exists():
        path.with_suffix(path.suffix + ".bak").write_text(text)
    pending = dict(changes)
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp_name, path.stat().st_mode & 0o777 if path.is_file() else 0o644)
    os.replace(tmp_name, path)


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    options = []
    for meta in OPTIONS:
        name = meta["name"]
        value = option(name)
        if name in INVALID_VALUES and name not in FROM_ENVIRONMENT:
            value = INVALID_VALUES[name]  # the bad line from the file, so any valid edit saves
        if STARTED_BY_APP and name == "PORT":
            source = "app"
        else:
            source = "env" if name in FROM_ENVIRONMENT else "file" if name in FILE_VALUES else "default"
        options.append({
            "name": name,
            "label": meta["label"],
            "help": meta["help"],
            "default": meta["default"],
            "value": value,
            "source": source,
            "editable": name not in FROM_ENVIRONMENT,
            "live": meta["live"],
            "restart_pending": not meta["live"] and value != STARTED_WITH[name],
            "missing": missing_paths(name, value),
            "problem": INVALID_OPTIONS.get(name),
        })
    return {"file": str(config_file_path()), "file_exists": config_file_path().is_file(),
            "options": options}


_config_lock = threading.Lock()


@app.put("/api/config")
def save_config(changes: dict[str, str]) -> dict[str, Any]:
    with _config_lock:
        return _save_config(changes)


def _save_config(changes: dict[str, str]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {}
    for name, value in changes.items():
        if name not in OPTION_BY_NAME:
            errors[name] = "Unknown option."
            continue
        if STARTED_BY_APP and name == "PORT":
            errors[name] = "Chosen by the Panorama app at each start."
            continue
        if name in FROM_ENVIRONMENT:
            errors[name] = "Set by an environment variable; change it there."
            continue
        value = value.strip()
        error = validate_option(name, value)
        if error:
            errors[name] = error
        else:
            cleaned[name] = value
    if errors:
        raise HTTPException(400, {"errors": errors})
    if cleaned and config_file_path().is_dir():
        raise HTTPException(409, {"errors": {}, "message":
            f"PANORAMA_ENV points to a folder, not a file: {config_file_path()}. Nothing was saved."})
    if cleaned:
        write_env_file(config_file_path(), cleaned)
        FILE_VALUES.update(cleaned)
        os.environ.update(cleaned)
        for name in cleaned:
            INVALID_OPTIONS.pop(name, None)
            INVALID_VALUES.pop(name, None)
        apply_live_options()
        _overview_cache["at"] = 0.0
    return get_config()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    """The same page; it shows the settings view when opened at /settings."""
    return FileResponse(HERE / "static" / "index.html")


init_db()


if __name__ == "__main__":
    import uvicorn

    # startup summary on stderr, so a quiet or empty board is explainable
    if ENV_FILE:
        print(f"panorama {__version__}: settings from {ENV_FILE}", file=sys.stderr)
    else:
        print(f"panorama {__version__}: no panorama.env found; using defaults "
              "(see panorama.env.example)", file=sys.stderr)
    wanted = os.environ.get("PANORAMA_ENV")
    if wanted and Path(wanted).expanduser().is_dir():
        print(f"  warning: PANORAMA_ENV={wanted} is a folder, not a file; no settings read, and Settings can't save", file=sys.stderr)
    elif wanted and not Path(wanted).expanduser().is_file():
        print(f"  PANORAMA_ENV={wanted} does not exist yet; Settings will create it", file=sys.stderr)
    for problem in INVALID_OPTIONS.values():
        print(f"  warning: {problem}", file=sys.stderr)
    for root in ROOTS:
        print(f"  scanning {root}{'' if root.is_dir() else '  (missing)'}", file=sys.stderr)
    for repo in EXTRA_REPOS:
        print(f"  repo     {repo}{'' if repo.is_dir() else '  (missing)'}", file=sys.stderr)
    if not any(root.is_dir() for root in ROOTS):
        print("  none of the roots exist: set PANORAMA_ROOTS", file=sys.stderr)

    if STARTED_BY_APP:
        # Panorama.app holds our stdin open. When the app goes away for any
        # reason (quit, crash, Force Quit), stdin closes and we exit too.
        import threading

        def exit_with_app() -> None:
            sys.stdin.read()
            os._exit(0)

        threading.Thread(target=exit_with_app, daemon=True).start()

    # The app's log would otherwise get a line per poll, forever.
    uvicorn.run(app, host="127.0.0.1", port=int(option("PORT")), access_log=not STARTED_BY_APP)
