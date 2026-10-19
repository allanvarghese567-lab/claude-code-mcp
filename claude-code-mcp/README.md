# claude-code-mcp

Remote MCP server: chat -> custom connector -> headless Claude Code -> branch -> PR.

## Setup
1. `npm install -g @anthropic-ai/claude-code` (server host needs the `claude` CLI)
2. `pip install -r requirements.txt`
3. Create a fine-grained GitHub PAT for `rag-project` only (Contents + Pull requests: read/write).
4. Create a GitHub OAuth App; callback URL = `<PUBLIC_BASE_URL>/auth/callback`.
5. Copy `.env.example` to `.env`, fill it in, then `export $(grep -v '^#' .env | xargs)`.
6. `python server.py` (serves MCP at `/mcp` on port 8000).
7. Put it behind HTTPS (Cloudflare Tunnel, Fly.io, Railway, a VPS with Caddy...).
8. claude.ai -> Settings -> Connectors -> Add custom connector -> `https://<host>/mcp`.

## Local test (no OAuth)
`ALLOW_UNAUTHENTICATED=1 python server.py` then `npx @modelcontextprotocol/inspector`.

## Guardrails built in
- Claude Code gets only Read/Edit/Write/Glob/Grep + `git rm`/`git mv`; no shell, no network tools.
- GitHub token is removed from Claude Code's environment.
- Work happens on `claude/<timestamp>` branches; only those can be pushed. `main` is never pushed to.
- Only `ALLOWED_GITHUB_USER` can call tools; one task runs at a time; turn and time limits.
- Also enable branch protection on `main` in GitHub.

## Docker
```
docker build -t claude-code-mcp .
docker run -d --name claude-code-mcp --env-file .env -p 8000:8000 --tmpfs /tmp claude-code-mcp
```
`--tmpfs /tmp` keeps the repo working copy in memory, so it vanishes when the container stops.
Secrets come from `--env-file` at run time; `.env` is excluded from git and from the image.

## Working copy lifecycle
- Cloned on the first task, reset to `origin/main` before every task.
- `push_and_open_pr` removes the local branch after the PR is created; `discard_branch` removes one you don't want.
- Anything left over disappears when the container is recreated.
