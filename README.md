# Codex Sessions

Codex is great until you need to find that one session you had last week, remember what you tried, and jump back in fast. Scrolling logs and guessing `codex resume` IDs doesn’t scale once you have dozens (or hundreds) of sessions.

Codex Sessions gives you a fast, local, full‑text “resume picker”: type a query, see live results, preview the conversation, and press Enter to resume the exact session.

## Features
- Indexes your local Codex session logs from `~/.codex/sessions/**/*.jsonl`
- Full‑text search via SQLite FTS5 (with recency bias)
- 2‑pane TUI: searchable list + preview pane (so you can tell sessions apart)
- One‑key resume: `Enter` runs `codex resume <session_id>`
- Personal organization: pin, tag, and add notes (stored locally)
- Filters: repo, cwd, tag, pinned‑only, and grouping of similar sessions
- Safe sharing helpers: export to Markdown (`export`) with best‑effort `--redact`

If this saves you time, star the repo so others can find it.

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
- Live UI: `codex_sessions` (type to search; results update live; Enter resumes)
- Start with a query: `codex_sessions "vllm"`
- Rebuild/refresh index: `codex_sessions_index` (add `--reindex` to force reparse)
- Plain output (no UI): `codex_sessions_search "query" --no-ui --limit 20`
- Browse most recent (no query): `codex_sessions_search --all`
- Export a session: `python3 ~/.codex-user/codex_sessions.py export <SESSION_ID> --out session.md --redact`

## Keybindings (live UI)
- Search: type; `Tab` toggles focus query/list; `Esc` clears query
- Navigate: `↑/↓`, `PgUp/PgDn`, `Home/End`
- Resume: `Enter`
- Organize: `x` pin/unpin, `t` tags, `n` note, `g` group similar titles
- Filter: `f` repo, `d` cwd, `F` tag, `P` pinned‑only
- Actions: `y` copy id, `c` copy resume command, `o` open session JSONL in `$EDITOR`, `R` force reindex
- Quit: `q`

## Data & privacy
- Index DB is stored locally (default: `~/.codex-user/codex_sessions.db`)
- The DB contains plain-text excerpts from your Codex sessions; do not commit or share it.
- Pins/tags/notes are also stored locally in the same DB.

## FAQ
### Does this upload my sessions anywhere?
No. It reads local Codex session files and writes a local SQLite DB for search.

### Why do I sometimes need `--reindex`?
Indexing is incremental by default (fast). Use `--reindex` (or press `R` in the UI) after upgrading the tool or if you want to rebuild everything from scratch.

## License
MIT (see `LICENSE`).
