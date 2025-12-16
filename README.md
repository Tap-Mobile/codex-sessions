# Codex Sessions (local full‑text search + resume)

Local SQLite FTS (full-text) search over Codex CLI interactive sessions, with a terminal UI that updates results live as you type and resumes the selected session.

## Features
- Indexes your local Codex session logs from `~/.codex/sessions/**/*.jsonl`
- Full-text search via SQLite FTS5
- Live search UI (type/edit query, see live results)
- `Enter` resumes: runs `codex resume <session_id>`
- Optional: copy session id to clipboard (macOS `pbcopy`)

## Install (macOS / zsh)
This tool is fully local; it does not send session content anywhere.

1) Copy files:
- `mkdir -p ~/.codex-user ~/.local/bin`
- `cp codex_sessions.py ~/.codex-user/codex_sessions.py`
- `cp codex_session_index.zsh ~/.codex-user/codex_session_index.zsh`
- `cp bin/codex_sessions* ~/.local/bin/`
- `chmod +x ~/.codex-user/codex_sessions.py ~/.local/bin/codex_sessions*`

2) Ensure `~/.local/bin` is on PATH (one-time). If you already use `~/.local/bin/env`, source it from a startup file such as `~/.zprofile`:
- `source "$HOME/.local/bin/env"`

3) Source helpers (optional; enables Zsh functions):
- `source "$HOME/.codex-user/codex_session_index.zsh"`

## Usage
- Live UI: `codex_sessions`
- Start with a query: `codex_sessions "vllm"`
- Rebuild/refresh index: `codex_sessions_index`
- Plain output (no UI): `codex_sessions_search "query" --no-ui --limit 20`
- Browse most recent (no query): `codex_sessions_search --all`

### Live UI keys
- Type to search (prefix search)
- `Enter` resume selected session
- `↑/↓` select
- `Backspace` edit query
- `Ctrl-U` clear query
- `c` copy session id (macOS)
- `p` print session id (no resume)
- `q` quit

## Data & privacy
- Index DB is stored locally (default: `~/.codex-user/codex_sessions.db`)
- The DB contains plain-text excerpts from your Codex sessions; do not commit or share it.

## License
MIT (see `LICENSE`).

