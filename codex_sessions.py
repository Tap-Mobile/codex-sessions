#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


DB_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  mtime_ns INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL,
  indexed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  cwd TEXT,
  cli_version TEXT,
  file_path TEXT NOT NULL,
  title TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS session_fts
USING fts5(
  session_id UNINDEXED,
  content,
  tokenize = 'porter',
  prefix = '2 3 4'
);
"""

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class SessionDoc:
    session_id: str
    created_at: int
    updated_at: int
    cwd: str
    cli_version: str
    file_path: str
    title: str
    content: str


def _epoch_seconds_from_iso(iso_ts: str) -> Optional[int]:
    try:
        # Example: 2025-11-03T08:59:27.319Z
        if iso_ts.endswith("Z"):
            iso_ts = iso_ts[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(iso_ts).timestamp())
    except Exception:
        return None


def _fmt_ts(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _extract_text_from_message_payload(payload: dict) -> str:
    parts: list[str] = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("input_text", "output_text"):
            txt = item.get("text")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt)
        elif t == "tool_result":
            # Keep tool output searchable but compact.
            txt = item.get("output") or item.get("text")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt)
    return "\n".join(parts).strip()


def parse_codex_session_file(path: Path) -> Optional[SessionDoc]:
    session_id: Optional[str] = None
    created_at: Optional[int] = None
    cwd = ""
    cli_version = ""

    updated_at: Optional[int] = None
    messages: list[tuple[str, str]] = []

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                ts = obj.get("timestamp")
                if isinstance(ts, str):
                    t = _epoch_seconds_from_iso(ts)
                    if t is not None:
                        updated_at = t if updated_at is None else max(updated_at, t)

                typ = obj.get("type")
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

                if typ == "session_meta":
                    sid = payload.get("id")
                    if isinstance(sid, str):
                        session_id = sid
                    created_iso = payload.get("timestamp")
                    if isinstance(created_iso, str):
                        created_at = _epoch_seconds_from_iso(created_iso)
                    cwd_val = payload.get("cwd")
                    if isinstance(cwd_val, str):
                        cwd = cwd_val
                    ver = payload.get("cli_version")
                    if isinstance(ver, str):
                        cli_version = ver
                    continue

                if typ != "response_item":
                    continue

                if payload.get("type") != "message":
                    continue

                role = payload.get("role")
                if role not in ("user", "assistant", "system"):
                    continue

                text = _extract_text_from_message_payload(payload)
                if text:
                    messages.append((role, text))

    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None

    if not session_id:
        return None

    # Fall back if timestamp missing.
    if created_at is None:
        created_at = updated_at or int(path.stat().st_mtime)
    if updated_at is None:
        updated_at = created_at

    title = ""
    for role, text in messages:
        if role == "user":
            title = " ".join(text.split())
            title = title[:120]
            break
    if not title:
        title = path.name

    content_lines: list[str] = []
    for role, text in messages:
        content_lines.append(f"{role}: {text}")
    content = "\n\n".join(content_lines).strip()

    return SessionDoc(
        session_id=session_id,
        created_at=int(created_at),
        updated_at=int(updated_at),
        cwd=cwd,
        cli_version=cli_version,
        file_path=str(path),
        title=title,
        content=content,
    )


def iter_session_files(codex_dir: Path) -> Iterable[Path]:
    root = codex_dir / "sessions"
    if not root.exists():
        return []
    return root.rglob("*.jsonl")


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(DB_SCHEMA)
    _migrate(conn)
    return conn


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    return str(row[0])


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _migrate(conn: sqlite3.Connection) -> None:
    with conn:
        v = _get_meta(conn, "schema_version")
        current = int(v) if v and v.isdigit() else 1
        if current >= SCHEMA_VERSION:
            return

        # v2: ensure session_fts uses prefix indexes for fast incremental searching.
        if current < 2:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS session_fts_v2
                USING fts5(
                  session_id UNINDEXED,
                  content,
                  tokenize = 'porter',
                  prefix = '2 3 4'
                )
                """
            )
            try:
                conn.execute(
                    "INSERT INTO session_fts_v2(session_id, content) SELECT session_id, content FROM session_fts"
                )
            except sqlite3.OperationalError:
                # If old table doesn't exist, that's fine.
                pass

            try:
                conn.execute("DROP TABLE session_fts")
            except sqlite3.OperationalError:
                pass
            conn.execute("ALTER TABLE session_fts_v2 RENAME TO session_fts")

        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))


def index_sessions(db_path: Path, codex_dir: Path) -> int:
    conn = connect_db(db_path)
    changed = 0
    now = int(dt.datetime.now().timestamp())

    files = sorted(iter_session_files(codex_dir))
    with conn:
        for p in files:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue

            row = conn.execute(
                "SELECT mtime_ns, size_bytes FROM files WHERE path = ?",
                (str(p),),
            ).fetchone()

            if row and int(row[0]) == int(st.st_mtime_ns) and int(row[1]) == int(st.st_size):
                continue

            doc = parse_codex_session_file(p)
            if doc is None:
                continue

            conn.execute(
                """
                INSERT INTO sessions(session_id, created_at, updated_at, cwd, cli_version, file_path, title)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                  created_at=excluded.created_at,
                  updated_at=excluded.updated_at,
                  cwd=excluded.cwd,
                  cli_version=excluded.cli_version,
                  file_path=excluded.file_path,
                  title=excluded.title
                """,
                (
                    doc.session_id,
                    doc.created_at,
                    doc.updated_at,
                    doc.cwd,
                    doc.cli_version,
                    doc.file_path,
                    doc.title,
                ),
            )

            conn.execute("DELETE FROM session_fts WHERE session_id = ?", (doc.session_id,))
            conn.execute(
                "INSERT INTO session_fts(session_id, content) VALUES(?, ?)",
                (doc.session_id, doc.content),
            )

            conn.execute(
                """
                INSERT INTO files(path, mtime_ns, size_bytes, indexed_at)
                VALUES(?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                  mtime_ns=excluded.mtime_ns,
                  size_bytes=excluded.size_bytes,
                  indexed_at=excluded.indexed_at
                """,
                (str(p), int(st.st_mtime_ns), int(st.st_size), now),
            )
            changed += 1

    conn.close()
    return changed


@dataclass(frozen=True)
class SearchRow:
    session_id: str
    created_at: int
    updated_at: int
    cwd: str
    title: str
    snippet: str
    score: float


def search_sessions(db_path: Path, query: str, limit: int) -> list[SearchRow]:
    conn = connect_db(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
          s.session_id,
          s.created_at,
          s.updated_at,
          COALESCE(s.cwd, '') AS cwd,
          COALESCE(s.title, '') AS title,
          snippet(session_fts, 1, '[', ']', '…', 14) AS snippet,
          bm25(session_fts) AS score
        FROM session_fts
        JOIN sessions s ON s.session_id = session_fts.session_id
        WHERE session_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    conn.close()

    out: list[SearchRow] = []
    for r in rows:
        out.append(
            SearchRow(
                session_id=r["session_id"],
                created_at=int(r["created_at"]),
                updated_at=int(r["updated_at"]),
                cwd=r["cwd"],
                title=r["title"],
                snippet=r["snippet"] or "",
                score=float(r["score"]),
            )
        )
    return out


def list_sessions(db_path: Path, limit: int) -> list[SearchRow]:
    conn = connect_db(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
          session_id,
          created_at,
          updated_at,
          COALESCE(cwd, '') AS cwd,
          COALESCE(title, '') AS title,
          '' AS snippet,
          0.0 AS score
        FROM sessions
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    out: list[SearchRow] = []
    for r in rows:
        out.append(
            SearchRow(
                session_id=r["session_id"],
                created_at=int(r["created_at"]),
                updated_at=int(r["updated_at"]),
                cwd=r["cwd"],
                title=r["title"],
                snippet=r["snippet"] or "",
                score=float(r["score"]),
            )
        )
    return out


def _escape_fts_token(token: str) -> str:
    token = token.replace('"', '""')
    return f'"{token}"'


def build_prefix_query(user_query: str) -> str:
    tokens = [t for t in user_query.strip().split() if t.strip()]
    if not tokens:
        return ""
    # Prefix-match every term for "live search while typing".
    return " ".join(f"{_escape_fts_token(t)}*" for t in tokens)


def _copy_to_clipboard(text: str) -> bool:
    if sys.platform == "darwin":
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            return False
    return False


def _truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    s = s.replace("\n", " ")
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"


def _run_curses_picker(rows: list[SearchRow], auto_copy: bool) -> Optional[str]:
    if not rows:
        return None

    def _ui(stdscr) -> Optional[str]:
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.keypad(True)

        idx = 0
        offset = 0
        copied = False

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            if height < 5 or width < 40:
                stdscr.addstr(0, 0, "Terminal too small.")
                stdscr.refresh()
                k = stdscr.getch()
                if k in (ord("q"), 27):
                    return None
                continue

            header = "↑/↓ select  Enter:resume  p:print id  c:copy id  r:print resume cmd  q:quit"
            if copied:
                header = header + " (copied)"
            stdscr.addstr(0, 0, _truncate(header, width - 1))

            visible = height - 2
            if idx < offset:
                offset = idx
            if idx >= offset + visible:
                offset = idx - visible + 1

            for row_i in range(visible):
                i = offset + row_i
                if i >= len(rows):
                    break
                r = rows[i]
                line = (
                    f"{_fmt_ts(r.created_at)}  "
                    f"{_fmt_ts(r.updated_at)}  "
                    f"{r.session_id}  "
                    f"{r.title}  "
                    f"{r.snippet}"
                )
                if i == idx:
                    stdscr.addstr(1 + row_i, 0, _truncate(line, width - 1), curses.A_REVERSE)
                else:
                    stdscr.addstr(1 + row_i, 0, _truncate(line, width - 1))

            stdscr.refresh()
            k = stdscr.getch()
            if k in (ord("q"), 27):
                return None
            if k in (curses.KEY_UP, ord("k")):
                idx = max(0, idx - 1)
            elif k in (curses.KEY_DOWN, ord("j")):
                idx = min(len(rows) - 1, idx + 1)
            elif k in (curses.KEY_ENTER, 10, 13):
                return f"__RESUME__ {rows[idx].session_id}"
            elif k in (ord("p"),):
                return rows[idx].session_id
            elif k in (ord("c"),):
                copied = _copy_to_clipboard(rows[idx].session_id)
            elif k in (ord("r"),):
                return f"codex resume {rows[idx].session_id}"

    selected = curses.wrapper(_ui)
    if selected and auto_copy:
        if selected.startswith("__RESUME__ "):
            pass
        elif selected.startswith("codex resume "):
            _copy_to_clipboard(selected.split()[-1])
        else:
            _copy_to_clipboard(selected)
    return selected


def _run_curses_live(db_path: Path, limit: int, initial_query: str, auto_copy: bool) -> Optional[str]:
    conn = connect_db(db_path)
    conn.row_factory = sqlite3.Row

    def _fetch_rows(q: str) -> list[SearchRow]:
        q = q.strip()
        try:
            if not q or q == "*":
                return list_sessions(db_path, limit)
            fts = build_prefix_query(q)
            if not fts:
                return list_sessions(db_path, limit)
            return search_sessions(db_path, fts, limit)
        except sqlite3.OperationalError:
            return []

    def _ui(stdscr) -> Optional[str]:
        curses.curs_set(1)
        stdscr.nodelay(False)
        stdscr.keypad(True)

        query = initial_query or ""
        cursor = len(query)
        rows = _fetch_rows(query)
        idx = 0
        offset = 0

        def _clamp_idx():
            nonlocal idx, offset
            if rows:
                idx = max(0, min(idx, len(rows) - 1))
            else:
                idx = 0
            offset = 0

        _clamp_idx()

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if height < 6 or width < 60:
                stdscr.addstr(0, 0, "Terminal too small (need ~60x6). Press q to quit.")
                stdscr.refresh()
                k = stdscr.getch()
                if k in (ord("q"), 27):
                    return None
                continue

            help_line = "Type to search (prefix). Enter:resume  ↑/↓ select  Backspace:edit  Ctrl-U:clear  p:print id  c:copy id  q:quit"
            stdscr.addstr(0, 0, _truncate(help_line, width - 1))

            prompt = "Query: "
            stdscr.addstr(1, 0, prompt)
            stdscr.addstr(1, len(prompt), _truncate(query, max(0, width - len(prompt) - 1)))
            # Cursor position within query line.
            cursor_x = min(len(prompt) + cursor, width - 1)
            stdscr.move(1, cursor_x)

            visible = height - 3
            if idx < offset:
                offset = idx
            if idx >= offset + visible:
                offset = idx - visible + 1

            if not rows:
                stdscr.addstr(3, 0, _truncate("(no matches)", width - 1))
            else:
                for row_i in range(visible):
                    i = offset + row_i
                    if i >= len(rows):
                        break
                    r = rows[i]
                    line = (
                        f"{_fmt_ts(r.created_at)}  "
                        f"{_fmt_ts(r.updated_at)}  "
                        f"{r.session_id}  "
                        f"{r.title}  "
                        f"{r.snippet}"
                    )
                    y = 2 + row_i
                    if i == idx:
                        stdscr.addstr(y, 0, _truncate(line, width - 1), curses.A_REVERSE)
                    else:
                        stdscr.addstr(y, 0, _truncate(line, width - 1))

            stdscr.refresh()
            k = stdscr.getch()

            if k in (ord("q"), 27):
                return None
            if k in (curses.KEY_UP, ord("k")):
                if rows:
                    idx = max(0, idx - 1)
                continue
            if k in (curses.KEY_DOWN, ord("j")):
                if rows:
                    idx = min(len(rows) - 1, idx + 1)
                continue
            if k in (curses.KEY_LEFT,):
                cursor = max(0, cursor - 1)
                continue
            if k in (curses.KEY_RIGHT,):
                cursor = min(len(query), cursor + 1)
                continue
            if k in (curses.KEY_HOME,):
                cursor = 0
                continue
            if k in (curses.KEY_END,):
                cursor = len(query)
                continue
            if k in (curses.KEY_BACKSPACE, 127, 8):
                if cursor > 0:
                    query = query[: cursor - 1] + query[cursor:]
                    cursor -= 1
                    rows = _fetch_rows(query)
                    idx = 0
                    offset = 0
                continue
            if k == curses.KEY_DC:
                if cursor < len(query):
                    query = query[:cursor] + query[cursor + 1 :]
                    rows = _fetch_rows(query)
                    idx = 0
                    offset = 0
                continue
            if k in (21,):  # Ctrl-U
                query = ""
                cursor = 0
                rows = _fetch_rows(query)
                idx = 0
                offset = 0
                continue
            if k in (curses.KEY_ENTER, 10, 13):
                if rows:
                    return f"__RESUME__ {rows[idx].session_id}"
                continue
            if k == ord("p"):
                if rows:
                    return rows[idx].session_id
                continue
            if k == ord("c"):
                if rows:
                    _copy_to_clipboard(rows[idx].session_id)
                continue

            # Printable characters.
            if 32 <= k <= 126:
                ch = chr(k)
                query = query[:cursor] + ch + query[cursor:]
                cursor += 1
                rows = _fetch_rows(query)
                idx = 0
                offset = 0
                continue

    selected = curses.wrapper(_ui)
    conn.close()
    if selected and auto_copy:
        if selected.startswith("__RESUME__ "):
            pass
        else:
            _copy_to_clipboard(selected)
    return selected


def cmd_index(args: argparse.Namespace) -> int:
    changed = index_sessions(args.db, args.codex_dir)
    if args.quiet:
        return 0
    print(f"Indexed {changed} updated/new session file(s) into {args.db}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not args.no_index:
        index_sessions(args.db, args.codex_dir)

    if args.all or not args.query or args.query.strip() in ("*", ""):
        rows = list_sessions(args.db, args.limit)
    else:
        rows = search_sessions(args.db, args.query, args.limit)
    if args.no_ui:
        for r in rows:
            print(
                f"{r.session_id}\t{_fmt_ts(r.created_at)}\t{_fmt_ts(r.updated_at)}\t{r.title}\t{r.snippet}"
            )
        return 0

    selected = _run_curses_picker(rows, auto_copy=args.copy)
    if not selected:
        return 1

    if selected.startswith("__RESUME__ "):
        session_id = selected.split(" ", 1)[1].strip()
        if args.copy:
            _copy_to_clipboard(session_id)
        os.execvp("codex", ["codex", "resume", session_id])
        return 1

    print(selected)
    if args.copy and selected.startswith("codex resume "):
        _copy_to_clipboard(selected.split()[-1])
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    if not args.no_index:
        index_sessions(args.db, args.codex_dir)

    selected = _run_curses_live(args.db, args.limit, args.query or "", auto_copy=args.copy)
    if not selected:
        return 1

    if selected.startswith("__RESUME__ "):
        session_id = selected.split(" ", 1)[1].strip()
        if args.copy:
            _copy_to_clipboard(session_id)
        os.execvp("codex", ["codex", "resume", session_id])
        return 1

    print(selected)
    return 0


def main(argv: list[str]) -> int:
    home = Path(os.path.expanduser("~"))
    default_codex_dir = home / ".codex"
    default_db = home / ".codex-user" / "codex_sessions.db"

    p = argparse.ArgumentParser(description="Full-text search over Codex sessions, with picker.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Index Codex sessions into SQLite FTS.")
    p_index.add_argument("--codex-dir", type=Path, default=default_codex_dir)
    p_index.add_argument("--db", type=Path, default=default_db)
    p_index.add_argument("--quiet", action="store_true")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search sessions (interactive picker by default).")
    p_search.add_argument("query", nargs="?", help="FTS query (use --all or '*' to browse).")
    p_search.add_argument("--codex-dir", type=Path, default=default_codex_dir)
    p_search.add_argument("--db", type=Path, default=default_db)
    p_search.add_argument("--limit", type=int, default=200)
    p_search.add_argument("--all", action="store_true", help="Browse most recent sessions (ignore query).")
    p_search.add_argument("--no-index", action="store_true", help="Skip auto indexing.")
    p_search.add_argument("--no-ui", action="store_true", help="Print results, no interactive picker.")
    p_search.add_argument("--copy", action="store_true", help="Copy selected id to clipboard (macOS pbcopy).")
    p_search.set_defaults(func=cmd_search)

    p_live = sub.add_parser("live", help="Interactive live search: type/edit query and see results update.")
    p_live.add_argument("query", nargs="?", help="Initial query (optional).")
    p_live.add_argument("--codex-dir", type=Path, default=default_codex_dir)
    p_live.add_argument("--db", type=Path, default=default_db)
    p_live.add_argument("--limit", type=int, default=200)
    p_live.add_argument("--no-index", action="store_true", help="Skip auto indexing.")
    p_live.add_argument("--copy", action="store_true", help="Copy selected id to clipboard (macOS pbcopy).")
    p_live.set_defaults(func=cmd_live)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
