"""
claude-code-mcp: a small remote MCP server that lets a Claude chat drive
headless Claude Code against ONE GitHub repo, safely (branch + PR only).

Tools
  run_task(instruction)          -> new branch claude/<ts>, Claude Code edits files, returns summary + diff stat
  get_diff(branch)               -> full diff of that branch vs main
  push_and_open_pr(branch, ...)  -> pushes the branch, opens a PR, then removes the local branch
  discard_branch(branch)         -> throws away a local branch you don't want
"""

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import httpx
from fastmcp import FastMCP

# ---------- config ----------
REPO = os.getenv("GITHUB_REPO", "allanvarghese567-lab/rag-project")
BASE_BRANCH = os.getenv("BASE_BRANCH", "main")
GITHUB_PAT = os.environ["GITHUB_PAT"]  # fine-grained PAT, this repo only
WORKDIR = Path(os.getenv("WORKDIR", "/tmp/rag-project-work"))
MAX_TURNS = int(os.getenv("CLAUDE_MAX_TURNS", "15"))
TASK_TIMEOUT = int(os.getenv("CLAUDE_TASK_TIMEOUT", "600"))  # seconds

# Claude Code may only read/edit/write files and use git rm / git mv (for deletes/renames).
ALLOWED_TOOLS = "Read,Edit,Write,Glob,Grep,Bash(git rm:*),Bash(git mv:*)"

BRANCH_RE = re.compile(r"^claude/\d+$")  # only branches this server created can be pushed
_lock = asyncio.Lock()  # one task at a time (single working copy)


# ---------- auth ----------
def build_auth():
    client_id = os.getenv("GITHUB_OAUTH_CLIENT_ID")
    if not client_id:
        if os.getenv("ALLOW_UNAUTHENTICATED") != "1":
            raise SystemExit(
                "Refusing to start without auth. Set GITHUB_OAUTH_CLIENT_ID/SECRET "
                "(or ALLOW_UNAUTHENTICATED=1 for local testing only)."
            )
        return None
    from fastmcp.server.auth.providers.github import GitHubProvider

    return GitHubProvider(
        client_id=client_id,
        client_secret=os.environ["GITHUB_OAUTH_CLIENT_SECRET"],
        base_url=os.environ["PUBLIC_BASE_URL"],  # e.g. https://mcp.example.com
    )


def require_allowed_user():
    """Only your own GitHub login may call the tools."""
    allowed = os.getenv("ALLOWED_GITHUB_USER")
    if not allowed:
        if os.getenv("ALLOW_UNAUTHENTICATED") == "1":
            return
        raise PermissionError("ALLOWED_GITHUB_USER is not set")
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    login = (getattr(token, "claims", None) or {}).get("login")
    if login != allowed:
        raise PermissionError("This GitHub account is not allowed to use this server")


mcp = FastMCP("rag-project-claude-code", auth=build_auth())


# ---------- helpers ----------
async def run(cmd, cwd=None, env=None, timeout=120):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Timed out after {timeout}s: {' '.join(cmd[:3])}...")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def auth_args():
    basic = base64.b64encode(f"x-access-token:{GITHUB_PAT}".encode()).decode()
    return ["-c", f"http.extraheader=Authorization: Basic {basic}"]


async def git(*args, remote=False, timeout=120):
    cmd = ["git"] + (auth_args() if remote else []) + list(args)
    code, out, err = await run(cmd, cwd=WORKDIR, timeout=timeout)
    if code != 0:
        # never leak the auth header back to the chat
        raise RuntimeError(f"git {args[0]} failed: {err.strip()[:500]}")
    return out


async def ensure_repo():
    if not (WORKDIR / ".git").exists():
        WORKDIR.parent.mkdir(parents=True, exist_ok=True)
        code, _, err = await run(
            ["git"] + auth_args() + ["clone", f"https://github.com/{REPO}.git", str(WORKDIR)],
            timeout=300,
        )
        if code != 0:
            raise RuntimeError(f"clone failed: {err.strip()[:500]}")
        await git("config", "user.name", "claude-code-mcp")
        await git("config", "user.email", "claude-code-mcp@users.noreply.github.com")
    await git("fetch", "origin", remote=True)
    await git("checkout", "-f", BASE_BRANCH)
    await git("reset", "--hard", f"origin/{BASE_BRANCH}")
    await git("clean", "-fd")


async def drop_local_branch(branch: str):
    await git("checkout", "-f", BASE_BRANCH)
    await git("branch", "-D", branch)


def check_branch(branch: str):
    if not BRANCH_RE.match(branch):
        raise ValueError("Only branches named claude/<number> created by run_task are allowed")


# ---------- tools ----------
@mcp.tool
async def run_task(instruction: str) -> str:
    """Run Claude Code on the repo with a plain-English instruction (edit, add,
    delete or refactor files). Changes go to a new branch claude/<timestamp>;
    nothing is pushed. Returns Claude Code's summary and a diff stat."""
    require_allowed_user()
    async with _lock:
        await ensure_repo()
        branch = f"claude/{int(time.time())}"
        await git("checkout", "-b", branch)

        # Claude Code must not see the GitHub token.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_PAT"}
        code, out, err = await run(
            [
                "claude", "-p", instruction,
                "--allowedTools", ALLOWED_TOOLS,
                "--max-turns", str(MAX_TURNS),
                "--output-format", "json",
            ],
            cwd=WORKDIR,
            env=env,
            timeout=TASK_TIMEOUT,
        )
        if code != 0:
            raise RuntimeError(f"claude exited {code}: {err.strip()[:500]}")
        try:
            summary = json.loads(out).get("result", out)
        except json.JSONDecodeError:
            summary = out

        await git("add", "-A")
        status = (await git("status", "--porcelain")).strip()
        if not status:
            return f"Branch {branch}: no file changes.\n\n{summary}"
        await git("commit", "-m", f"Claude Code: {instruction[:60]}")
        stat = await git("diff", "--stat", f"{BASE_BRANCH}...{branch}")
        return f"Branch: {branch}\n\n{summary}\n\nChanges:\n{stat}"


@mcp.tool
async def get_diff(branch: str) -> str:
    """Show the full diff of a claude/<timestamp> branch against main."""
    require_allowed_user()
    check_branch(branch)
    async with _lock:
        diff = await git("diff", f"{BASE_BRANCH}...{branch}")
    return diff[:20000] + ("\n...[truncated]" if len(diff) > 20000 else "")


@mcp.tool
async def push_and_open_pr(branch: str, title: str, body: str = "") -> str:
    """Push a claude/<timestamp> branch and open a pull request into main.
    Call this only after the user has reviewed the diff."""
    require_allowed_user()
    check_branch(branch)
    async with _lock:
        await git("push", "origin", branch, remote=True)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://api.github.com/repos/{REPO}/pulls",
            headers={
                "Authorization": f"Bearer {GITHUB_PAT}",
                "Accept": "application/vnd.github+json",
            },
            json={"title": title, "body": body, "head": branch, "base": BASE_BRANCH},
        )
    if r.status_code >= 300:
        raise RuntimeError(f"PR creation failed: {r.status_code} {r.text[:300]}")
    async with _lock:
        await drop_local_branch(branch)  # the branch now lives on GitHub; free the local copy
    return r.json()["html_url"]


@mcp.tool
async def discard_branch(branch: str) -> str:
    """Delete a local claude/<timestamp> branch (and its uncommitted-to-GitHub
    changes) when you don't want to keep the result."""
    require_allowed_user()
    check_branch(branch)
    async with _lock:
        await drop_local_branch(branch)
    return f"Discarded {branch}"


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
