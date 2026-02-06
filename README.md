# Codex Sessions

Codex is great until you need to find that one session you had last week, remember what you tried, and jump back in fast. Scrolling logs and guessing `codex resume` IDs doesn’t scale once you have dozens (or hundreds) of sessions.

Codex Sessions gives you a fast, local, full‑text “resume picker”: type a query, see live results, preview the conversation, and press Enter to resume the exact session.

## Choose your setup

### Option 1: Run instantly (recommended)
```bash
npx codex-sessions
```

### Option 2: Install globally (npm)
```bash
npm i -g codex-sessions
codex-sessions
```

Uninstall:
```bash
npm uninstall -g codex-sessions
```

### Option 3: Manual install (shell scripts + zsh helpers)
```bash
mkdir -p ~/.codex-user ~/.local/bin
cp codex_sessions.py ~/.codex-user/codex_sessions.py
cp codex_session_index.zsh ~/.codex-user/codex_session_index.zsh
cp bin/codex_sessions* ~/.local/bin/
chmod +x ~/.codex-user/codex_sessions.py ~/.local/bin/codex_sessions*
```

Ensure `~/.local/bin` is on PATH (one-time):
```bash
source "$HOME/.local/bin/env"
```

If this saves you time, star the repo so others can find it.

## What it does
- Indexes your local Codex session logs from `~/.codex/sessions/**/*.jsonl`
- Indexes messages + tool calls + tool outputs (so you can search for commands, paths, errors, etc.)
- Full‑text search via SQLite FTS5 (with recency bias)
- 2‑pane TUI: searchable list + pannable preview pane
- `Enter` resumes: runs `codex resume <session_id>`
- Pin/tag/note + filters (repo/cwd/tag/pinned-only) + grouping

## Usage
### Session picker
- `codex-sessions` (or `codex_sessions`) opens the live UI
- Type to search, preview on the right, `Enter` to resume

### Fork (private) / Share (portable)
- Fork (full context, starts a new Codex session): `python3 ~/.codex-user/codex_sessions.py fork <SESSION_ID> --cd`
- Share as a local file (redacted by default): `python3 ~/.codex-user/codex_sessions.py share <SESSION_ID> --method file`
- Share as a private Gist (redacted by default): `python3 ~/.codex-user/codex_sessions.py share <SESSION_ID> --method gist`
- Import a pack: `python3 ~/.codex-user/codex_sessions.py import ./codex-session-<ID>.md --cd`

### Quick start (npx)
- `npx codex-sessions` then type a query, press Enter to resume.

## Multi-agent (tmux)
If you want a “manager + sub-agents” workflow (each agent in its own tmux window), use `codex-agents`.

### Start 3 agents
```bash
codex-agents start --agents 3
codex-agents attach
```

### Start agents in a repo directory
```bash
codex-agents start --agents 3 --cd ~/github/Tap-Mobile/photoboost-android
```

### Send a task to an agent window
```bash
codex-agents send agent1 "Task: …"
```

### Check status / stop
```bash
codex-agents status
codex-agents stop
```

## Keybindings (live UI)
- Type to search (live, no Enter). `Backspace` deletes; `Ctrl+U` clears.
- `Enter` resume selected session
- `Tab` toggle focus `list` / `preview` (`↑/↓` / `PgUp/PgDn` / `Home/End` act on focused pane)
- `←/→` pan preview horizontally (when wrap is off and preview is focused)
- `Esc` clears query; if query is empty, exits (also `Ctrl+C` exits)

### Ctrl+X commands
Press `Ctrl+X` then:
- `x` pin/unpin
- `t` tags
- `m` note
- `f` repo filter, `d` cwd filter, `F` tag filter
- `P` pinned-only, `g` group
- `y` copy id, `c` copy resume cmd, `o` open JSONL in `$EDITOR`
- `K` fork, `S` share, `R` reindex
- `w` toggle wrap, `v` toggle tail mode
- `n` / `N` next/prev preview hit

## Data & privacy
- Index DB is stored locally (default: `~/.codex-user/codex_sessions.db`)
- The DB contains plain-text excerpts from your Codex sessions; do not commit it.
- Pins/tags/notes are also stored locally in the same DB.

### Fork vs share
- **Fork** starts a *new* Codex session with the original transcript as context (private, full context).
- **Share** creates a portable “session pack” and defaults to redacting obvious secrets; you can share it as a local file or a private GitHub Gist.

## FAQ
### Does this upload my sessions anywhere?
No. It reads local Codex session files and writes a local SQLite DB for search.

### Why do I sometimes need `--reindex`?
Indexing is incremental by default (fast). Use `--reindex` (or press `R` in the UI) after upgrading the tool or if you want to rebuild everything from scratch.

## License
MIT (see `LICENSE`).
