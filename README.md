# Codex Sessions (local full‑text search + resume)

Local SQLite FTS (full-text) search over Codex CLI interactive sessions, with a terminal UI that updates results live as you type and resumes the selected session.

## Features
- Indexes your local Codex session logs from `~/.codex/sessions/**/*.jsonl`
- Full-text search via SQLite FTS5 + recency bias
- 2-pane live UI: searchable list + preview pane
- `Enter` resumes: runs `codex resume <session_id>`
- Pins/tags/notes stored locally (never committed)
- Filters: by repo/cwd/tag and “pinned only”
- Optional export to Markdown (`export`) with best-effort `--redact`

## Install (macOS / zsh)
This tool is fully local; it does not send session content anywhere.

1) Copy files:
- `mkdir -p ~/.codex-user ~/.local/bin`
- `cp codex_sessions.py ~/.codex-user/codex_sessions.py`
- `cp codex_session_index.zsh ~/.codex-user/codex_session_index.zsh`
- `cp bin/codex_sessions* ~/.local/bin/`
- `chmod +x ~/.codex-user/codex_sessions.py ~/.local/bin/codex_sessions*`

2) Ensure `~/.local/bin` is on PATH (one-time). If you already use `~/.local/bin/env`, source it from `~/.zprofile` or `~/.zshrc`:
- `source "$HOME/.local/bin/env"`

3) Source helpers (optional; enables Zsh functions):
- `source "$HOME/.codex-user/codex_session_index.zsh"`

## Usage
- Live UI: `codex_sessions` (type to search; results update live)
- Start with a query: `codex_sessions "vllm"`
- Rebuild/refresh index: `codex_sessions_index` (add `--reindex` to force reparse)
- Plain output (no UI): `codex_sessions_search "query" --no-ui --limit 20`
- Browse most recent (no query): `codex_sessions_search --all`
- Export a session: `python3 ~/.codex-user/codex_sessions.py export <SESSION_ID> --out session.md --redact`

### Live UI keys
- Type to search (prefix search), `Tab` toggles focus query/list
- `Enter` resume selected session
- `↑/↓`, `PgUp/PgDn`, `Home/End` navigate
- `Esc` clear query
- `x` pin/unpin, `t` tags, `n` note
- `f` repo filter, `d` cwd filter, `F` tag filter, `P` pinned-only, `g` group similar titles
- `y` copy id, `c` copy resume command, `o` open session JSONL in `$EDITOR`
- `R` force reindex, `q` quit

## Data & privacy
- Index DB is stored locally (default: `~/.codex-user/codex_sessions.db`)
- The DB contains plain-text excerpts from your Codex sessions; do not commit or share it.
- Pins/tags/notes are also stored locally in the same DB.

## License
MIT (see `LICENSE`).
