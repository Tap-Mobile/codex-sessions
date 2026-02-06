#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, replace
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
  title TEXT,
  preview TEXT,
  repo_root TEXT,
  repo_name TEXT,
  repo_branch TEXT,
  repo_sha TEXT
);

CREATE TABLE IF NOT EXISTS user_sessions (
  session_id TEXT PRIMARY KEY,
  pinned INTEGER NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS session_fts
USING fts5(
  session_id UNINDEXED,
  content,
  tokenize = 'porter',
  prefix = '2 3 4'
);
"""

SCHEMA_VERSION = 4
PARSER_VERSION = 6


@dataclass(frozen=True)
class SessionDoc:
    session_id: str
    created_at: int
    updated_at: int
    cwd: str
    cli_version: str
    file_path: str
    title: str
    preview: str
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

def _strip_boilerplate(text: str) -> str:
    s = text
    s = re.sub(r"<environment_context>.*?</environment_context>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<user_instructions>.*?</user_instructions>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<instructions>.*?</instructions>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"^#\s*AGENTS\.md.*$", "", s, flags=re.IGNORECASE | re.MULTILINE)
    s = s.replace("\r", "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    # Preserve newlines for preview; FTS doesn't need whitespace collapsing.
    s = "\n".join(line.rstrip() for line in s.splitlines()).strip()
    return s


def _maybe_parse_json_dict(s: object) -> Optional[dict]:
    if not isinstance(s, str):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def parse_codex_session_file(path: Path) -> Optional[SessionDoc]:
    session_id: Optional[str] = None
    created_at: Optional[int] = None
    cwd = ""
    cli_version = ""

    updated_at: Optional[int] = None
    messages: list[tuple[str, str]] = []
    tool_name_by_call_id: dict[str, str] = {}

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

                payload_type = payload.get("type")

                if payload_type == "message":
                    role = payload.get("role")
                    # Index the actual conversation, not Codex's internal prompts.
                    if role not in ("user", "assistant"):
                        continue

                    text = _extract_text_from_message_payload(payload)
                    if not text:
                        continue

                    # Strip common boilerplate in user messages (AGENTS, environment context, etc.).
                    if role == "user":
                        text = _strip_boilerplate(text)
                        if not text:
                            continue

                    messages.append((role, text))
                    continue

                # Newer Codex logs record tool activity as standalone response items.
                if payload_type == "function_call":
                    name = payload.get("name") if isinstance(payload.get("name"), str) else ""
                    call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else ""
                    args = payload.get("arguments")
                    args_dict = _maybe_parse_json_dict(args)
                    if call_id and name:
                        tool_name_by_call_id[call_id] = name

                    tool_text = ""
                    if args_dict and name == "exec_command":
                        cmd = args_dict.get("cmd")
                        if isinstance(cmd, str) and cmd.strip():
                            tool_text = cmd.strip()
                    if not tool_text:
                        tool_text = args if isinstance(args, str) else (json.dumps(args_dict) if args_dict else "")
                    if tool_text:
                        messages.append((f"tool_call {name or 'unknown'}", tool_text))
                    continue

                if payload_type == "function_call_output":
                    call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else ""
                    name = tool_name_by_call_id.get(call_id, "")
                    out = payload.get("output")
                    out_text = out if isinstance(out, str) else ""
                    if out_text:
                        label = f"tool_output {name}" if name else "tool_output"
                        messages.append((label, out_text))
                    continue

                # Custom tools (e.g., apply_patch) are stored separately.
                if payload_type == "custom_tool_call":
                    name = payload.get("name") if isinstance(payload.get("name"), str) else ""
                    call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else ""
                    inp = payload.get("input")
                    if call_id and name:
                        tool_name_by_call_id[call_id] = name
                    inp_text = inp if isinstance(inp, str) else ""
                    if inp_text:
                        messages.append((f"tool_call {name or 'unknown'}", inp_text))
                    continue

                if payload_type == "custom_tool_call_output":
                    call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else ""
                    name = tool_name_by_call_id.get(call_id, "")
                    out = payload.get("output")
                    out_text = ""
                    if isinstance(out, str):
                        out_dict = _maybe_parse_json_dict(out)
                        if out_dict and isinstance(out_dict.get("output"), str):
                            out_text = out_dict["output"]
                        else:
                            out_text = out
                    if out_text:
                        label = f"tool_output {name}" if name else "tool_output"
                        messages.append((label, out_text))
                    continue

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

    user_messages: list[str] = []
    for role, text in messages:
        if role == "user":
            if len(text) < 8:
                continue
            user_messages.append(text)
    title = user_messages[0][:200] if user_messages else ""
    if not title:
        title = path.name
    preview = ""
    if user_messages:
        preview = user_messages[-1][:240]

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
        preview=preview,
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

        # v3: add per-session preview for browse mode.
        if current < 3:
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN preview TEXT")
            except sqlite3.OperationalError:
                pass
        # v4: repo metadata + user session annotations.
        if current < 4:
            for col in ("repo_root", "repo_name", "repo_branch", "repo_sha"):
                try:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_sessions (
                  session_id TEXT PRIMARY KEY,
                  pinned INTEGER NOT NULL DEFAULT 0,
                  tags TEXT NOT NULL DEFAULT '',
                  note TEXT NOT NULL DEFAULT '',
                  updated_at INTEGER NOT NULL
                )
                """
            )

        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))


def index_sessions(db_path: Path, codex_dir: Path, force: bool = False) -> int:
    conn = connect_db(db_path)
    changed = 0
    now = int(dt.datetime.now().timestamp())
    force_reindex = False
    git_cache: dict[str, tuple[str, str, str, str]] = {}

    with conn:
        pv = _get_meta(conn, "parser_version")
        current = int(pv) if pv and pv.isdigit() else 0
        if current != PARSER_VERSION:
            force_reindex = True
            _set_meta(conn, "parser_version", str(PARSER_VERSION))
        if force:
            force_reindex = True

    files = sorted(iter_session_files(codex_dir))
    with conn:
        _set_meta(conn, "last_index_started_at", str(now))
        _set_meta(conn, "last_index_reason", "forced" if force else ("parser_bump" if force_reindex else "incremental"))
        for p in files:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue

            row = conn.execute(
                "SELECT mtime_ns, size_bytes FROM files WHERE path = ?",
                (str(p),),
            ).fetchone()

            if (
                not force_reindex
                and row
                and int(row[0]) == int(st.st_mtime_ns)
                and int(row[1]) == int(st.st_size)
            ):
                continue

            doc = parse_codex_session_file(p)
            if doc is None:
                continue

            repo_root = ""
            repo_name = ""
            repo_branch = ""
            repo_sha = ""
            if doc.cwd:
                if doc.cwd in git_cache:
                    repo_root, repo_name, repo_branch, repo_sha = git_cache[doc.cwd]
                else:
                    try:
                        proc = subprocess.run(
                            ["git", "-C", doc.cwd, "rev-parse", "--show-toplevel"],
                            check=False,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            text=True,
                        )
                        top = (proc.stdout or "").strip() if proc.returncode == 0 else ""
                        if top:
                            repo_root = top
                            repo_name = Path(top).name
                            b = subprocess.run(
                                ["git", "-C", doc.cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                                check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                text=True,
                            )
                            s = subprocess.run(
                                ["git", "-C", doc.cwd, "rev-parse", "--short", "HEAD"],
                                check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                text=True,
                            )
                            repo_branch = (b.stdout or "").strip() if b.returncode == 0 else ""
                            repo_sha = (s.stdout or "").strip() if s.returncode == 0 else ""
                    except Exception:
                        pass
                    git_cache[doc.cwd] = (repo_root, repo_name, repo_branch, repo_sha)

            conn.execute(
                """
                INSERT INTO sessions(
                  session_id, created_at, updated_at, cwd, cli_version, file_path, title, preview,
                  repo_root, repo_name, repo_branch, repo_sha
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                  created_at=excluded.created_at,
                  updated_at=excluded.updated_at,
                  cwd=excluded.cwd,
                  cli_version=excluded.cli_version,
                  file_path=excluded.file_path,
                  title=excluded.title,
                  preview=excluded.preview,
                  repo_root=excluded.repo_root,
                  repo_name=excluded.repo_name,
                  repo_branch=excluded.repo_branch,
                  repo_sha=excluded.repo_sha
                """,
                (
                    doc.session_id,
                    doc.created_at,
                    doc.updated_at,
                    doc.cwd,
                    doc.cli_version,
                    doc.file_path,
                    doc.title,
                    doc.preview,
                    repo_root,
                    repo_name,
                    repo_branch,
                    repo_sha,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO user_sessions(session_id, updated_at) VALUES(?, ?)",
                (doc.session_id, now),
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

        _set_meta(conn, "last_index_finished_at", str(int(time.time())))

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
    pinned: int
    tags: str
    note: str
    file_path: str
    repo_name: str
    repo_branch: str
    repo_sha: str


def search_sessions(db_path: Path, query: str, limit: int, now: Optional[int] = None) -> list[SearchRow]:
    conn = connect_db(db_path)
    conn.row_factory = sqlite3.Row
    now_val = int(now or time.time())
    recency_weight = 0.15
    rows = conn.execute(
        """
        SELECT
          s.session_id,
          s.created_at,
          s.updated_at,
          COALESCE(s.cwd, '') AS cwd,
          COALESCE(s.title, '') AS title,
          snippet(session_fts, 1, '[', ']', '…', 28) AS snippet,
          (bm25(session_fts) + ((? - s.updated_at) / 86400.0) * ?) AS score,
          COALESCE(u.pinned, 0) AS pinned,
          COALESCE(u.tags, '') AS tags,
          COALESCE(u.note, '') AS note,
          COALESCE(s.file_path, '') AS file_path,
          COALESCE(s.repo_name, '') AS repo_name,
          COALESCE(s.repo_branch, '') AS repo_branch,
          COALESCE(s.repo_sha, '') AS repo_sha
        FROM session_fts
        JOIN sessions s ON s.session_id = session_fts.session_id
        LEFT JOIN user_sessions u ON u.session_id = s.session_id
        WHERE session_fts MATCH ?
        ORDER BY pinned DESC, score
        LIMIT ?
        """,
        (now_val, recency_weight, query, limit),
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
                pinned=int(r["pinned"]),
                tags=r["tags"] or "",
                note=r["note"] or "",
                file_path=r["file_path"] or "",
                repo_name=r["repo_name"] or "",
                repo_branch=r["repo_branch"] or "",
                repo_sha=r["repo_sha"] or "",
            )
        )
    return out


def list_sessions(db_path: Path, limit: int) -> list[SearchRow]:
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
          COALESCE(s.preview, '') AS snippet,
          0.0 AS score,
          COALESCE(u.pinned, 0) AS pinned,
          COALESCE(u.tags, '') AS tags,
          COALESCE(u.note, '') AS note,
          COALESCE(s.file_path, '') AS file_path,
          COALESCE(s.repo_name, '') AS repo_name,
          COALESCE(s.repo_branch, '') AS repo_branch,
          COALESCE(s.repo_sha, '') AS repo_sha
        FROM sessions s
        LEFT JOIN user_sessions u ON u.session_id = s.session_id
        ORDER BY pinned DESC, s.updated_at DESC
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
                pinned=int(r["pinned"]),
                tags=r["tags"] or "",
                note=r["note"] or "",
                file_path=r["file_path"] or "",
                repo_name=r["repo_name"] or "",
                repo_branch=r["repo_branch"] or "",
                repo_sha=r["repo_sha"] or "",
            )
        )
    return out


def build_prefix_query(user_query: str) -> str:
    # Extract "words" so queries like "TaskModal.tsx" and "/path/to/foo-bar"
    # are searchable without requiring FTS syntax.
    tokens = re.findall(r"[A-Za-z0-9_]+", user_query or "")
    if not tokens:
        return ""
    # Prefix-match every term for "live search while typing".
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if not t:
            continue
        is_kw = t.upper() in ("AND", "OR", "NOT", "NEAR")
        term = f'"{t}"' if is_kw else t
        if term in seen:
            continue
        seen.add(term)
        out.append(f"{term}*" if not is_kw else term)
    return " ".join(out)


def _copy_to_clipboard(text: str) -> bool:
    if sys.platform == "darwin":
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            return False
    return False


def _have_gh() -> bool:
    try:
        res = subprocess.run(["gh", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return res.returncode == 0
    except Exception:
        return False


def _gh_authed() -> bool:
    try:
        res = subprocess.run(["gh", "auth", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return res.returncode == 0
    except Exception:
        return False


def _maybe_redact(text: str, redact: bool) -> str:
    return _redact_text(text) if redact else text


def _session_pack_paths(session_id: str, out_dir: Path) -> tuple[Path, Path]:
    base = out_dir / f"codex-session-{session_id}"
    return base.with_suffix(".json"), base.with_suffix(".md")


def _build_pack(conn: sqlite3.Connection, session_id: str, *, redact: bool) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT
          s.session_id,
          s.created_at,
          s.updated_at,
          COALESCE(s.cwd,'') AS cwd,
          COALESCE(s.file_path,'') AS file_path,
          COALESCE(s.repo_root,'') AS repo_root,
          COALESCE(s.repo_name,'') AS repo_name,
          COALESCE(s.repo_branch,'') AS repo_branch,
          COALESCE(s.repo_sha,'') AS repo_sha,
          COALESCE(u.pinned,0) AS pinned,
          COALESCE(u.tags,'') AS tags,
          COALESCE(u.note,'') AS note,
          COALESCE(session_fts.content,'') AS content
        FROM sessions s
        LEFT JOIN user_sessions u ON u.session_id = s.session_id
        LEFT JOIN session_fts ON session_fts.session_id = s.session_id
        WHERE s.session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if not row:
        raise ValueError("session not found")

    content = _maybe_redact(row["content"] or "", redact)
    cwd = _maybe_redact(row["cwd"] or "", redact)
    file_path = _maybe_redact(row["file_path"] or "", redact)

    return {
        "schema": 1,
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "session_id": row["session_id"],
        "cwd": cwd,
        "file_path": file_path,
        "repo": {
            "root": _maybe_redact(row["repo_root"] or "", redact),
            "name": row["repo_name"] or "",
            "branch": row["repo_branch"] or "",
            "sha": row["repo_sha"] or "",
        },
        "annotations": {
            "pinned": int(row["pinned"]),
            "tags": row["tags"] or "",
            "note": _maybe_redact(row["note"] or "", redact),
        },
        "content": content,
        "redacted": bool(redact),
    }


def _pack_to_markdown(pack: dict) -> str:
    repo = pack.get("repo") or {}
    ann = pack.get("annotations") or {}
    lines = [
        f"# Codex session pack: {pack.get('session_id','')}",
        "",
        f"- Created: {_fmt_ts(int(pack.get('created_at', 0)))}",
        f"- Updated: {_fmt_ts(int(pack.get('updated_at', 0)))}",
        f"- Repo: {repo.get('name') or '-'} ({repo.get('branch') or '-'} @ {repo.get('sha') or '-'})",
        f"- CWD: {pack.get('cwd') or '-'}",
        f"- Tags: {ann.get('tags') or '-'}",
        f"- Note: {ann.get('note') or '-'}",
        f"- Redacted: {pack.get('redacted')}",
        "",
        "## Transcript",
        "",
        "```",
        pack.get("content", "") or "",
        "```",
        "",
    ]
    return "\n".join(lines)


def _truncate_for_prompt(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _preview_build_render_lines(
    raw_lines: list[str], *, width: int, wrap: bool, x_offset: int
) -> tuple[list[str], list[int]]:
    """
    Build a render buffer for the preview pane.

    Returns:
      - rendered_lines: list of lines already wrapped/truncated/panned to width
      - raw_to_render: mapping of raw line index -> starting rendered line index
    """
    w = max(1, int(width))
    x = max(0, int(x_offset))

    rendered: list[str] = []
    raw_to_render: list[int] = []

    for line in raw_lines:
        raw_to_render.append(len(rendered))
        s = (line or "").replace("\r", "")
        if wrap:
            # For wrap mode we ignore horizontal panning; it doesn't compose well.
            if not s:
                rendered.append("")
                continue
            for i in range(0, len(s), w):
                rendered.append(s[i : i + w])
            continue

        if x:
            s = s[x:]
        rendered.append(s[:w])

    return rendered, raw_to_render


def _preview_find_matches(lines: list[str], terms: list[str]) -> list[tuple[int, int]]:
    """
    Return raw-line match locations sorted by "best" match:
      - lines matching more terms first
      - then earlier lines
      - then earlier column
    """
    ts = [t.lower() for t in (terms or []) if isinstance(t, str) and t.strip()]
    if not ts:
        return []

    out: list[tuple[int, int, int]] = []
    for i, line in enumerate(lines):
        s = (line or "").lower()
        hits = 0
        first_col: Optional[int] = None
        for t in ts:
            pos = s.find(t)
            if pos == -1:
                continue
            hits += 1
            if first_col is None or pos < first_col:
                first_col = pos
        if hits:
            out.append((i, int(first_col or 0), hits))

    out.sort(key=lambda x: (-x[2], x[0], x[1]))
    return [(i, col) for (i, col, _hits) in out]


def cmd_fork(args: argparse.Namespace) -> int:
    if not args.no_index:
        index_sessions(args.db, args.codex_dir, force=args.reindex)

    conn = connect_db(args.db)
    try:
        pack = _build_pack(conn, args.session_id, redact=False)
    except ValueError as e:
        conn.close()
        print(str(e), file=sys.stderr)
        return 2
    conn.close()

    # Write a local pack (private by default).
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json, out_md = _session_pack_paths(args.session_id, out_dir)
    out_json.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_pack_to_markdown(pack), encoding="utf-8")

    content_for_prompt = _truncate_for_prompt(pack.get("content", "") or "", args.max_chars)
    prompt = (
        "You are continuing work from a forked Codex session.\n"
        f"Original session id: {args.session_id}\n"
        f"Pack saved at: {out_md}\n\n"
        "Use the transcript below as full context.\n\n"
        "=== TRANSCRIPT (verbatim) ===\n"
        f"{content_for_prompt}\n"
        "=== END TRANSCRIPT ===\n"
    )
    if args.user_prompt:
        prompt = prompt + "\nUser request for this fork:\n" + args.user_prompt.strip() + "\n"

    cwd = pack.get("cwd") or ""
    cmd = ["codex"]
    if cwd and args.cd and Path(cwd).exists():
        cmd += ["-C", cwd]
    cmd.append(prompt)
    os.execvp("codex", cmd)
    return 1


def cmd_share(args: argparse.Namespace) -> int:
    if not args.no_index:
        index_sessions(args.db, args.codex_dir, force=args.reindex)

    conn = connect_db(args.db)
    try:
        pack = _build_pack(conn, args.session_id, redact=not args.no_redact)
    except ValueError as e:
        conn.close()
        print(str(e), file=sys.stderr)
        return 2
    conn.close()

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json, out_md = _session_pack_paths(args.session_id, out_dir)
    out_json.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_pack_to_markdown(pack), encoding="utf-8")

    if args.method == "file":
        print(str(out_md))
        return 0

    if args.method == "gist":
        if not _have_gh():
            print("gh not found; falling back to local file", file=sys.stderr)
            print(str(out_md))
            return 0
        if not _gh_authed():
            print("gh not authenticated; run `gh auth login` then retry. Falling back to local file.", file=sys.stderr)
            print(str(out_md))
            return 0

        title = args.title or f"codex-session-{args.session_id}"
        res = subprocess.run(
            ["gh", "gist", "create", "--private", "--desc", title, str(out_md)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if res.returncode != 0:
            print(res.stderr.strip() or "failed to create gist", file=sys.stderr)
            print(str(out_md))
            return 2
        url = (res.stdout or "").strip()
        if url:
            _copy_to_clipboard(url)
            print(url)
            return 0
        print(str(out_md))
        return 0

    print("unknown share method", file=sys.stderr)
    return 2


def cmd_import(args: argparse.Namespace) -> int:
    p = Path(args.path).expanduser()
    if not p.exists():
        print("file not found", file=sys.stderr)
        return 2
    if p.suffix.lower() == ".json":
        pack = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    else:
        # Treat as markdown; use whole file as context.
        pack = {"session_id": "imported", "cwd": "", "content": p.read_text(encoding="utf-8", errors="replace")}

    content_for_prompt = _truncate_for_prompt(pack.get("content", "") or "", args.max_chars)
    prompt = (
        "You are continuing work from an imported Codex session pack.\n"
        f"Source: {p}\n\n"
        "Use the transcript below as full context.\n\n"
        "=== TRANSCRIPT (verbatim) ===\n"
        f"{content_for_prompt}\n"
        "=== END TRANSCRIPT ===\n"
    )
    if args.user_prompt:
        prompt = prompt + "\nUser request:\n" + args.user_prompt.strip() + "\n"

    cmd = ["codex"]
    if args.cd and isinstance(pack.get("cwd"), str) and pack.get("cwd") and Path(pack["cwd"]).exists():
        cmd += ["-C", pack["cwd"]]
    cmd.append(prompt)
    os.execvp("codex", cmd)
    return 1

def _truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    s = s.replace("\n", " ")
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"

def _format_table_row(r: SearchRow, width: int, include_snippet: bool) -> str:
    created = _fmt_ts(r.created_at)
    updated = _fmt_ts(r.updated_at)
    sid = (r.session_id[:7] + ("★" if getattr(r, "pinned", 0) else " ")).ljust(8)

    # Fixed columns + spaces:
    # 16 +1 +16 +1 +8 +1 = 43, leaving remainder for title/snippet.
    remaining = max(0, width - 1 - 43)
    title_w = min(80, max(22, remaining // (2 if include_snippet else 1)))
    snippet_w = max(0, remaining - title_w - (1 if include_snippet else 0))

    title = _truncate(r.title or "", title_w)
    snippet = _truncate(r.snippet or "", snippet_w) if include_snippet else ""
    if include_snippet and snippet_w > 0:
        return f"{created:<16} {updated:<16} {sid:<8} {title:<{title_w}} {snippet}"
    return f"{created:<16} {updated:<16} {sid:<8} {title}"

def _format_table_header(width: int, include_snippet: bool) -> tuple[str, str]:
    remaining = max(0, width - 1 - 43)
    title_w = min(80, max(22, remaining // (2 if include_snippet else 1)))
    snippet_w = max(0, remaining - title_w - (1 if include_snippet else 0))
    if include_snippet and snippet_w > 0:
        hdr = f"{'CREATED':<16} {'UPDATED':<16} {'ID★':<8} {'TITLE':<{title_w}} {'MATCH':<{snippet_w}}"
    else:
        hdr = f"{'CREATED':<16} {'UPDATED':<16} {'ID★':<8} {'TITLE':<{title_w}}"
    return _truncate(hdr, width - 1), _truncate("-" * (width - 1), width - 1)


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

            visible = height - 4
            if idx < offset:
                offset = idx
            if idx >= offset + visible:
                offset = idx - visible + 1

            header, sep = _format_table_header(width, include_snippet=True)
            stdscr.addstr(1, 0, header)
            stdscr.addstr(2, 0, sep)

            for row_i in range(visible):
                i = offset + row_i
                if i >= len(rows):
                    break
                r = rows[i]
                line = _format_table_row(r, width, include_snippet=True)
                if i == idx:
                    stdscr.addstr(3 + row_i, 0, _truncate(line, width - 1), curses.A_REVERSE)
                else:
                    stdscr.addstr(3 + row_i, 0, _truncate(line, width - 1))

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

    def _get_meta_str(key: str) -> str:
        try:
            v = _get_meta(conn, key)
            return v or ""
        except Exception:
            return ""

    def _wrap(text: str, w: int) -> list[str]:
        if w <= 1:
            return [""]
        words = (text or "").replace("\r", "").split()
        lines: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for word in words:
            add = len(word) + (1 if cur else 0)
            if cur_len + add <= w:
                cur.append(word)
                cur_len += add
                continue
            if cur:
                lines.append(" ".join(cur))
            cur = [word]
            cur_len = len(word)
        if cur:
            lines.append(" ".join(cur))
        return lines or [""]

    def _prompt(stdscr, prompt: str, initial: str = "") -> Optional[str]:
        height, width = stdscr.getmaxyx()
        y = height - 1
        stdscr.move(y, 0)
        stdscr.clrtoeol()
        stdscr.addstr(y, 0, _truncate(prompt, width - 1))
        stdscr.refresh()
        curses.echo()
        try:
            buf = stdscr.getstr(y, min(len(prompt), width - 1), max(1, width - len(prompt) - 1))
        except Exception:
            curses.noecho()
            return None
        curses.noecho()
        try:
            s = buf.decode("utf-8", errors="replace")
        except Exception:
            s = str(buf)
        s = s.strip()
        if not s and initial:
            return initial
        return s

    def _group_rows(rows: list[SearchRow]) -> list[SearchRow]:
        grouped: dict[str, tuple[SearchRow, int]] = {}
        for r in rows:
            key = re.sub(r"\s+", " ", (r.title or "").strip().lower())
            if not key:
                key = r.session_id
            if key not in grouped:
                grouped[key] = (r, 1)
                continue
            cur, n = grouped[key]
            best = r if r.updated_at >= cur.updated_at else cur
            grouped[key] = (best, n + 1)
        out: list[SearchRow] = []
        for best, n in grouped.values():
            if n <= 1:
                out.append(best)
            else:
                out.append(replace(best, title=f"{best.title} (+{n-1})"))
        out.sort(key=lambda r: (r.pinned, r.updated_at), reverse=True)
        return out

    def _apply_filters(
        rows: list[SearchRow], repo: str, cwd: str, tag: str, pinned_only: bool, group_mode: bool
    ) -> list[SearchRow]:
        out: list[SearchRow] = []
        for r in rows:
            if pinned_only and not r.pinned:
                continue
            if repo and repo.lower() not in (r.repo_name or "").lower():
                continue
            if cwd and cwd.lower() not in (r.cwd or "").lower():
                continue
            if tag and tag.lower() not in (r.tags or "").lower():
                continue
            out.append(r)
        return _group_rows(out) if group_mode else out

    def _fetch_rows(q: str, repo: str, cwd: str, tag: str, pinned_only: bool, group_mode: bool) -> list[SearchRow]:
        q = (q or "").strip()
        try:
            base: list[SearchRow]
            if not q or q == "*":
                base = list_sessions(db_path, limit)
            else:
                fts = build_prefix_query(q)
                base = search_sessions(db_path, fts, limit) if fts else list_sessions(db_path, limit)
            return _apply_filters(base, repo, cwd, tag, pinned_only, group_mode)
        except sqlite3.OperationalError:
            return []

    def _get_detail(session_id: str) -> dict:
        row = conn.execute(
            """
            SELECT
              s.session_id,
              s.created_at,
              s.updated_at,
              COALESCE(s.cwd,'') AS cwd,
              COALESCE(s.title,'') AS title,
              COALESCE(s.file_path,'') AS file_path,
              COALESCE(s.repo_name,'') AS repo_name,
              COALESCE(s.repo_branch,'') AS repo_branch,
              COALESCE(s.repo_sha,'') AS repo_sha,
              COALESCE(u.pinned,0) AS pinned,
              COALESCE(u.tags,'') AS tags,
              COALESCE(u.note,'') AS note,
              COALESCE(session_fts.content,'') AS content
            FROM sessions s
            LEFT JOIN user_sessions u ON u.session_id = s.session_id
            LEFT JOIN session_fts ON session_fts.session_id = s.session_id
            WHERE s.session_id = ?
            """,
            (session_id,),
        ).fetchone()
        return dict(row) if row else {}

    def _set_user_field(session_id: str, field: str, value) -> None:
        now = int(time.time())
        with conn:
            conn.execute("INSERT OR IGNORE INTO user_sessions(session_id, updated_at) VALUES(?, ?)", (session_id, now))
            conn.execute(f"UPDATE user_sessions SET {field} = ?, updated_at = ? WHERE session_id = ?", (value, now, session_id))

    def _ui(stdscr) -> Optional[str]:
        curses.curs_set(1)
        stdscr.nodelay(False)
        stdscr.keypad(True)

        query = initial_query or ""
        cursor = len(query)
        focus = "query"  # "query" | "list" | "preview"

        filter_repo = ""
        filter_cwd = ""
        filter_tag = ""
        pinned_only = False
        group_mode = False

        rows = _fetch_rows(query, filter_repo, filter_cwd, filter_tag, pinned_only, group_mode)
        idx = 0
        offset = 0
        detail_cache: dict[str, dict] = {}
        status_msg = ""
        preview_tail = True
        preview_wrap = False
        preview_x_offset = 0
        preview_y_offset = 0
        preview_match_idx = 0

        preview_cached_sid = ""
        preview_cached_terms: tuple[str, ...] = ()
        preview_cached_width = 0
        preview_cached_wrap = preview_wrap
        preview_cached_x = preview_x_offset
        preview_raw_lines: list[str] = []
        preview_render_lines: list[str] = []
        preview_raw_to_render: list[int] = []
        preview_matches_raw: list[tuple[int, int]] = []
        preview_matches_render: list[int] = []

        def _clamp():
            nonlocal idx, offset
            if rows:
                idx = max(0, min(idx, len(rows) - 1))
            else:
                idx = 0
            if idx < offset:
                offset = idx

        def _refresh_rows(reset_selection: bool = False):
            nonlocal rows, idx, offset
            rows = _fetch_rows(query, filter_repo, filter_cwd, filter_tag, pinned_only, group_mode)
            if reset_selection:
                idx = 0
                offset = 0
            _clamp()

        def _selected_id() -> str:
            if not rows:
                return ""
            return rows[idx].session_id

        def _query_terms() -> tuple[str, ...]:
            # Keep it simple and predictable: extract "word" tokens.
            raw = re.findall(r"[A-Za-z0-9_]+", query or "")
            seen: set[str] = set()
            out: list[str] = []
            for t in raw:
                if len(t) < 2:
                    continue
                key = t.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(t)
            return tuple(out)

        def _preview_ensure(detail: dict, preview_w: int, preview_h: int) -> None:
            nonlocal preview_cached_sid
            nonlocal preview_cached_terms
            nonlocal preview_cached_width
            nonlocal preview_cached_wrap
            nonlocal preview_cached_x
            nonlocal preview_raw_lines
            nonlocal preview_render_lines
            nonlocal preview_raw_to_render
            nonlocal preview_matches_raw
            nonlocal preview_matches_render
            nonlocal preview_match_idx
            nonlocal preview_y_offset

            sid = _selected_id()
            if not sid:
                preview_cached_sid = ""
                preview_cached_terms = ()
                preview_raw_lines = []
                preview_render_lines = []
                preview_raw_to_render = []
                preview_matches_raw = []
                preview_matches_render = []
                preview_match_idx = 0
                preview_y_offset = 0
                return

            terms = _query_terms()
            sid_changed = sid != preview_cached_sid
            terms_changed = terms != preview_cached_terms

            if sid_changed or not preview_raw_lines:
                content = detail.get("content", "") or ""
                preview_raw_lines = content.splitlines()

            need_render_rebuild = (
                sid_changed
                or not preview_render_lines
                or int(preview_w) != int(preview_cached_width)
                or bool(preview_wrap) != bool(preview_cached_wrap)
                or int(preview_x_offset) != int(preview_cached_x)
            )
            if need_render_rebuild:
                preview_render_lines, preview_raw_to_render = _preview_build_render_lines(
                    preview_raw_lines,
                    width=max(1, int(preview_w)),
                    wrap=bool(preview_wrap),
                    x_offset=int(preview_x_offset),
                )
                preview_cached_width = int(preview_w)
                preview_cached_wrap = bool(preview_wrap)
                preview_cached_x = int(preview_x_offset)

            if sid_changed or terms_changed or need_render_rebuild:
                preview_matches_raw = _preview_find_matches(preview_raw_lines, list(terms))
                preview_matches_render = [
                    preview_raw_to_render[i]
                    for (i, _col) in preview_matches_raw
                    if 0 <= i < len(preview_raw_to_render)
                ]

            if sid_changed or terms_changed:
                preview_match_idx = 0
                if terms and preview_matches_render:
                    preview_y_offset = max(0, int(preview_matches_render[0]) - 2)
                else:
                    if preview_tail:
                        preview_y_offset = max(0, len(preview_render_lines) - max(1, int(preview_h)))
                    else:
                        preview_y_offset = 0

            max_y = max(0, len(preview_render_lines) - max(1, int(preview_h)))
            preview_y_offset = max(0, min(int(preview_y_offset), int(max_y)))

            if preview_matches_render:
                preview_match_idx = max(0, min(int(preview_match_idx), len(preview_matches_render) - 1))
            else:
                preview_match_idx = 0

            preview_cached_sid = sid
            preview_cached_terms = terms

        _clamp()

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if height < 12 or width < 110:
                stdscr.addstr(0, 0, "Terminal too small (need ~110x12). Press q to quit.")
                stdscr.refresh()
                k = stdscr.getch()
                if k in (ord("q"), 27):
                    return None
                continue

            left_w = max(60, int(width * 0.56))
            right_w = width - left_w - 1
            list_top = 3
            list_bottom = height - 2
            list_h = max(1, list_bottom - list_top)

            help_line = "Enter resume | Tab focus q/l/p | / query | arrows/Pg: list or preview | x pin | t tags | m note | f repo | d cwd | F tag | P pinned | g group | n/N hit | w wrap | v tail | y id | c cmd | o open | S share | K fork | R reindex | Esc clear | q quit"
            stdscr.addstr(0, 0, _truncate(help_line, width - 1))

            prompt = f"Query({focus}): "
            stdscr.addstr(1, 0, _truncate(prompt, left_w - 1))
            stdscr.addstr(1, len(prompt), _truncate(query, max(0, left_w - len(prompt) - 1)))
            if focus == "query":
                stdscr.move(1, min(len(prompt) + cursor, left_w - 1))

            # Filters/status line.
            idx_info = f"{idx+1}/{len(rows)}" if rows else "0/0"
            filt_bits = []
            if filter_repo:
                filt_bits.append(f"repo~{filter_repo}")
            if filter_cwd:
                filt_bits.append(f"cwd~{filter_cwd}")
            if filter_tag:
                filt_bits.append(f"tag~{filter_tag}")
            if pinned_only:
                filt_bits.append("pinned")
            if group_mode:
                filt_bits.append("grouped")
            filt = " | ".join(filt_bits) if filt_bits else "no filters"
            indexed_at = _get_meta_str("last_index_finished_at") or "?"
            try:
                indexed_at_h = _fmt_ts(int(indexed_at))
            except Exception:
                indexed_at_h = "?"
            status_base = f"{idx_info} | {filt} | indexed {indexed_at_h}"
            status = status_base
            if status_msg:
                status = status + f" | {status_msg}"
            stdscr.addstr(2, 0, _truncate(status, width - 1))

            # Draw vertical separator.
            for y in range(3, height - 1):
                stdscr.addstr(y, left_w, "|")

            # Left header and list.
            header, sep = _format_table_header(left_w, include_snippet=False)
            stdscr.addstr(3, 0, header)
            stdscr.addstr(4, 0, sep)

            visible = max(1, list_h - 2)
            if idx < offset:
                offset = idx
            if idx >= offset + visible:
                offset = idx - visible + 1

            for row_i in range(visible):
                i = offset + row_i
                if i >= len(rows):
                    break
                r = rows[i]
                line = _format_table_row(r, left_w, include_snippet=False)
                y = 5 + row_i
                if i == idx:
                    stdscr.addstr(y, 0, _truncate(line, left_w - 1), curses.A_REVERSE)
                else:
                    stdscr.addstr(y, 0, _truncate(line, left_w - 1))

            # Right preview.
            sid = _selected_id()
            detail = {}
            if sid:
                if sid not in detail_cache:
                    detail_cache[sid] = _get_detail(sid)
                detail = detail_cache.get(sid) or {}

            rx = left_w + 1
            preview_w = max(1, right_w - 1)
            preview_label = "PREVIEW" if focus != "preview" else "PREVIEW*"
            stdscr.addstr(3, rx, _truncate(preview_label, preview_w))
            stdscr.addstr(4, rx, _truncate("-" * preview_w, preview_w))
            if not detail:
                stdscr.addstr(5, rx, _truncate("(no selection)", preview_w))
            else:
                meta_lines = [
                    f"id: {detail.get('session_id','')}",
                    f"created: {_fmt_ts(int(detail.get('created_at',0)))}  updated: {_fmt_ts(int(detail.get('updated_at',0)))}",
                    f"repo: {detail.get('repo_name','') or '-'}  branch: {detail.get('repo_branch','') or '-'}  sha: {detail.get('repo_sha','') or '-'}",
                    f"cwd: {detail.get('cwd','') or '-'}",
                    f"tags: {detail.get('tags','') or '-'}",
                    f"note: {detail.get('note','') or '-'}",
                ]
                y = 5
                for ml in meta_lines:
                    if y >= height - 2:
                        break
                    for wline in _wrap(ml, preview_w):
                        if y >= height - 2:
                            break
                        stdscr.addstr(y, rx, _truncate(wline, preview_w))
                        y += 1

                if y < height - 2:
                    stdscr.addstr(y, rx, _truncate("-" * preview_w, preview_w))
                    y += 1

                preview_h = max(1, (height - 2) - y)
                _preview_ensure(detail, preview_w, preview_h)

                # Redraw status line with preview info (computed after ensure).
                preview_bits: list[str] = []
                preview_bits.append("wrap" if preview_wrap else "nowrap")
                if not preview_wrap and preview_x_offset:
                    preview_bits.append(f"x{preview_x_offset}")
                if preview_cached_terms:
                    if preview_matches_render:
                        preview_bits.append(f"hits {preview_match_idx+1}/{len(preview_matches_render)}")
                    else:
                        preview_bits.append("hits 0")
                status2 = status_base + (" | preview " + " ".join(preview_bits) if preview_bits else "")
                if status_msg:
                    status2 = status2 + f" | {status_msg}"
                stdscr.addstr(2, 0, _truncate(status2, width - 1))

                cur_hit = -1
                if preview_matches_render:
                    try:
                        cur_hit = int(preview_matches_render[preview_match_idx])
                    except Exception:
                        cur_hit = -1

                start = int(preview_y_offset)
                end = min(len(preview_render_lines), start + preview_h)
                for i, line in enumerate(preview_render_lines[start:end]):
                    yy = y + i
                    if yy >= height - 2:
                        break
                    attr = curses.A_BOLD if (start + i) == cur_hit else 0
                    stdscr.addstr(yy, rx, _truncate(line, preview_w), attr)

            stdscr.refresh()
            k = stdscr.getch()
            status_msg = ""

            if k in (ord("q"),):
                return None
            if k in (27,):  # ESC
                query = ""
                cursor = 0
                _refresh_rows(reset_selection=True)
                continue
            if k in (9,):  # TAB
                if focus == "query":
                    focus = "list"
                elif focus == "list":
                    focus = "preview"
                else:
                    focus = "query"
                continue
            if k in (ord("/"),):
                focus = "query"
                continue

            # Preview navigation only when focused.
            if focus == "preview" and detail:
                if k in (curses.KEY_UP, ord("k"), 16):  # Ctrl-P
                    preview_y_offset = max(0, int(preview_y_offset) - 1)
                    continue
                if k in (curses.KEY_DOWN, ord("j"), 14):  # Ctrl-N
                    preview_y_offset = int(preview_y_offset) + 1
                    _preview_ensure(detail, preview_w, preview_h)
                    continue
                if k == curses.KEY_NPAGE:  # PgDn
                    preview_y_offset = int(preview_y_offset) + int(preview_h)
                    _preview_ensure(detail, preview_w, preview_h)
                    continue
                if k == curses.KEY_PPAGE:  # PgUp
                    preview_y_offset = max(0, int(preview_y_offset) - int(preview_h))
                    continue
                if k == curses.KEY_HOME:
                    preview_y_offset = 0
                    continue
                if k == curses.KEY_END:
                    preview_y_offset = max(0, len(preview_render_lines) - int(preview_h))
                    continue
                if k == curses.KEY_LEFT:
                    if not preview_wrap and preview_x_offset > 0:
                        preview_x_offset = max(0, int(preview_x_offset) - 1)
                        _preview_ensure(detail, preview_w, preview_h)
                    continue
                if k == curses.KEY_RIGHT:
                    if not preview_wrap:
                        preview_x_offset = int(preview_x_offset) + 1
                        _preview_ensure(detail, preview_w, preview_h)
                    continue
                if k == ord("w"):
                    preview_wrap = not preview_wrap
                    if preview_wrap:
                        preview_x_offset = 0
                    _preview_ensure(detail, preview_w, preview_h)
                    continue
                if k == ord("n"):
                    if preview_matches_render:
                        preview_match_idx = (int(preview_match_idx) + 1) % len(preview_matches_render)
                        preview_y_offset = max(0, int(preview_matches_render[preview_match_idx]) - 2)
                        _preview_ensure(detail, preview_w, preview_h)
                    continue
                if k == ord("N"):
                    if preview_matches_render:
                        preview_match_idx = (int(preview_match_idx) - 1) % len(preview_matches_render)
                        preview_y_offset = max(0, int(preview_matches_render[preview_match_idx]) - 2)
                        _preview_ensure(detail, preview_w, preview_h)
                    continue
                if k == ord("v"):
                    preview_tail = not preview_tail
                    if preview_tail and not preview_cached_terms:
                        preview_y_offset = max(0, len(preview_render_lines) - int(preview_h))
                    continue

            # List navigation (arrow keys always; vim/Ctrl keys only when not typing).
            if focus != "preview":
                if k in (curses.KEY_UP,):
                    if rows:
                        idx = max(0, idx - 1)
                        _clamp()
                    continue
                if k in (curses.KEY_DOWN,):
                    if rows:
                        idx = min(len(rows) - 1, idx + 1)
                        _clamp()
                    continue
                if focus != "query" and k in (ord("k"), 16):  # Ctrl-P
                    if rows:
                        idx = max(0, idx - 1)
                        _clamp()
                    continue
                if focus != "query" and k in (ord("j"), 14):  # Ctrl-N
                    if rows:
                        idx = min(len(rows) - 1, idx + 1)
                        _clamp()
                    continue
                if k == curses.KEY_NPAGE:  # PgDn
                    if rows:
                        idx = min(len(rows) - 1, idx + visible)
                        _clamp()
                    continue
                if k == curses.KEY_PPAGE:  # PgUp
                    if rows:
                        idx = max(0, idx - visible)
                        _clamp()
                    continue
                if k == curses.KEY_HOME:
                    idx = 0
                    offset = 0
                    continue
                if k == curses.KEY_END and rows:
                    idx = len(rows) - 1
                    _clamp()
                    continue

            sid = _selected_id()
            if k in (curses.KEY_ENTER, 10, 13):
                if sid:
                    return f"__RESUME__ {sid}"
                continue

            # Query edit/typing takes precedence over action hotkeys.
            if focus == "query":
                if k in (curses.KEY_LEFT,):
                    cursor = max(0, cursor - 1)
                    continue
                if k in (curses.KEY_RIGHT,):
                    cursor = min(len(query), cursor + 1)
                    continue
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    if cursor > 0:
                        query = query[: cursor - 1] + query[cursor:]
                        cursor -= 1
                        _refresh_rows(reset_selection=True)
                    continue
                if k == curses.KEY_DC:
                    if cursor < len(query):
                        query = query[:cursor] + query[cursor + 1 :]
                        _refresh_rows(reset_selection=True)
                    continue
                if k in (21,):  # Ctrl-U
                    query = ""
                    cursor = 0
                    _refresh_rows(reset_selection=True)
                    continue
                if 32 <= k <= 126:
                    ch = chr(k)
                    query = query[:cursor] + ch + query[cursor:]
                    cursor += 1
                    _refresh_rows(reset_selection=True)
                    continue
                continue

            # From here down: focus is list/preview, so action hotkeys are active.
            if k == ord("y") and sid:
                _copy_to_clipboard(sid)
                status_msg = "copied id"
                continue
            if k == ord("c") and sid:
                _copy_to_clipboard(f"codex resume {sid}")
                status_msg = "copied cmd"
                continue
            if k == ord("o") and sid:
                fp = (detail or {}).get("file_path", "") if detail else ""
                if not fp:
                    fp = rows[idx].file_path
                if fp:
                    return f"__OPEN__ {fp}"
                status_msg = "no file_path"
                continue

            if k == ord("K") and sid:
                return f"__FORK__ {sid}"

            if k == ord("S") and sid:
                return f"__SHARE__ {sid}"

            if k == ord("x") and sid:
                new_val = 0 if rows[idx].pinned else 1
                _set_user_field(sid, "pinned", new_val)
                if sid in detail_cache:
                    detail_cache.pop(sid, None)
                _refresh_rows()
                status_msg = "pinned" if new_val else "unpinned"
                continue
            if k == ord("t") and sid:
                current_tags = (detail or {}).get("tags", "") if detail else rows[idx].tags
                new_tags = _prompt(stdscr, "tags (space/comma separated): ", current_tags)
                if new_tags is not None:
                    _set_user_field(sid, "tags", new_tags)
                    detail_cache.pop(sid, None)
                    _refresh_rows()
                continue
            if k == ord("m") and sid:
                current_note = (detail or {}).get("note", "") if detail else rows[idx].note
                new_note = _prompt(stdscr, "note: ", current_note)
                if new_note is not None:
                    _set_user_field(sid, "note", new_note)
                    detail_cache.pop(sid, None)
                continue

            if k == ord("f"):
                filter_repo = _prompt(stdscr, "filter repo (empty clears): ", filter_repo) or ""
                _refresh_rows(reset_selection=True)
                continue
            if k == ord("d"):
                filter_cwd = _prompt(stdscr, "filter cwd contains (empty clears): ", filter_cwd) or ""
                _refresh_rows(reset_selection=True)
                continue
            if k == ord("F"):
                filter_tag = _prompt(stdscr, "filter tag contains (empty clears): ", filter_tag) or ""
                _refresh_rows(reset_selection=True)
                continue
            if k == ord("P"):
                pinned_only = not pinned_only
                _refresh_rows(reset_selection=True)
                continue
            if k == ord("g"):
                group_mode = not group_mode
                _refresh_rows(reset_selection=True)
                continue

            if k == ord("R"):
                # Force reindex.
                index_sessions(db_path, Path(os.path.expanduser("~")) / ".codex", force=True)
                detail_cache.clear()
                _refresh_rows(reset_selection=True)
                status_msg = "reindexed"
                continue
            if k == ord("v"):
                preview_tail = not preview_tail
                if preview_tail and detail and not preview_cached_terms:
                    preview_y_offset = max(0, len(preview_render_lines) - int(preview_h))
                continue

            if k == ord("n"):
                if detail and preview_matches_render:
                    preview_match_idx = (int(preview_match_idx) + 1) % len(preview_matches_render)
                    preview_y_offset = max(0, int(preview_matches_render[preview_match_idx]) - 2)
                    _preview_ensure(detail, preview_w, preview_h)
                continue
            if k == ord("N"):
                if detail and preview_matches_render:
                    preview_match_idx = (int(preview_match_idx) - 1) % len(preview_matches_render)
                    preview_y_offset = max(0, int(preview_matches_render[preview_match_idx]) - 2)
                    _preview_ensure(detail, preview_w, preview_h)
                continue

            # When focus=list/preview, ignore typing.

    selected = curses.wrapper(_ui)
    conn.close()
    if selected and auto_copy:
        if selected.startswith("__RESUME__ "):
            pass
        else:
            _copy_to_clipboard(selected)
    return selected


def cmd_index(args: argparse.Namespace) -> int:
    changed = index_sessions(args.db, args.codex_dir, force=args.reindex)
    if args.quiet:
        return 0
    print(f"Indexed {changed} updated/new session file(s) into {args.db}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not args.no_index:
        index_sessions(args.db, args.codex_dir, force=args.reindex)

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
        index_sessions(args.db, args.codex_dir, force=args.reindex)

    selected = _run_curses_live(args.db, args.limit, args.query or "", auto_copy=args.copy)
    if not selected:
        return 1

    if selected.startswith("__RESUME__ "):
        session_id = selected.split(" ", 1)[1].strip()
        if args.copy:
            _copy_to_clipboard(session_id)
        os.execvp("codex", ["codex", "resume", session_id])
        return 1

    if selected.startswith("__OPEN__ "):
        path = selected.split(" ", 1)[1].strip()
        editor = os.environ.get("EDITOR") or "vi"
        os.execvp(editor, [editor, path])
        return 1

    if selected.startswith("__FORK__ "):
        session_id = selected.split(" ", 1)[1].strip()
        os.execvp(
            "python3",
            [
                "python3",
                str(Path(__file__).resolve()),
                "fork",
                session_id,
                "--cd",
            ],
        )
        return 1

    if selected.startswith("__SHARE__ "):
        session_id = selected.split(" ", 1)[1].strip()
        method = "file"
        if _have_gh() and _gh_authed():
            method = "gist"
        os.execvp("python3", ["python3", str(Path(__file__).resolve()), "share", session_id, "--method", method])
        return 1

    print(selected)
    return 0


def _redact_text(text: str) -> str:
    s = text
    s = s.replace(str(Path.home()), "~")
    s = re.sub(r"(?i)/Users/[^/\\s]+", "/Users/<REDACTED>", s)
    s = re.sub(r"(?i)/home/[^/\\s]+", "/home/<REDACTED>", s)

    # Common credential formats.
    s = re.sub(r"ghp_[A-Za-z0-9]{30,}", "ghp_<REDACTED>", s)
    s = re.sub(r"gho_[A-Za-z0-9_]{20,}", "gho_<REDACTED>", s)
    s = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "github_pat_<REDACTED>", s)
    s = re.sub(r"sk-[A-Za-z0-9]{20,}", "sk-<REDACTED>", s)
    s = re.sub(r"xox[baprs]-[A-Za-z0-9-]{20,}", "xox<REDACTED>", s)
    s = re.sub(r"AIza[0-9A-Za-z\\-_]{35}", "AIza<REDACTED>", s)
    s = re.sub(r"AKIA[0-9A-Z]{16}", "AKIA<REDACTED>", s)
    s = re.sub(r"ASIA[0-9A-Z]{16}", "ASIA<REDACTED>", s)

    # Header-style tokens.
    s = re.sub(r"(?i)Authorization:\\s*Bearer\\s+[^\\s]+", "Authorization: Bearer <REDACTED>", s)
    s = re.sub(r"(?i)\\bBearer\\s+[A-Za-z0-9\\._\\-]{20,}\\b", "Bearer <REDACTED>", s)

    # Key-value secrets.
    s = re.sub(
        r"(?i)\\b(secret|token|password|passwd|api[_-]?key|access[_-]?key|session[_-]?token)\\b\\s*[:=]\\s*[^\\s\\\"']+",
        r"\\1=<REDACTED>",
        s,
    )

    # Emails + IPs.
    s = re.sub(r"\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}\\b", "<REDACTED_EMAIL>", s)
    s = re.sub(r"\\b\\d{1,3}(?:\\.\\d{1,3}){3}\\b", "<REDACTED_IP>", s)
    return s


def cmd_export(args: argparse.Namespace) -> int:
    conn = connect_db(args.db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT
          s.session_id,
          s.created_at,
          s.updated_at,
          COALESCE(s.cwd,'') AS cwd,
          COALESCE(s.repo_name,'') AS repo_name,
          COALESCE(s.repo_branch,'') AS repo_branch,
          COALESCE(s.repo_sha,'') AS repo_sha,
          COALESCE(u.tags,'') AS tags,
          COALESCE(u.note,'') AS note,
          COALESCE(session_fts.content,'') AS content
        FROM sessions s
        LEFT JOIN user_sessions u ON u.session_id = s.session_id
        LEFT JOIN session_fts ON session_fts.session_id = s.session_id
        WHERE s.session_id = ?
        """,
        (args.session_id,),
    ).fetchone()
    conn.close()
    if not row:
        print("session not found", file=sys.stderr)
        return 2

    content = row["content"] or ""
    if args.redact:
        content = _redact_text(content)

    out = [
        f"# Codex session {row['session_id']}",
        "",
        f"- Created: {_fmt_ts(int(row['created_at']))}",
        f"- Updated: {_fmt_ts(int(row['updated_at']))}",
        f"- Repo: {row['repo_name'] or '-'} ({row['repo_branch'] or '-' } @ {row['repo_sha'] or '-'})",
        f"- CWD: {row['cwd'] or '-'}",
        f"- Tags: {row['tags'] or '-'}",
        f"- Note: {row['note'] or '-'}",
        "",
        "## Transcript",
        "",
        "```",
        content,
        "```",
        "",
    ]
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        try:
            sys.stdout.write(text)
        except BrokenPipeError:
            return 0
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
    p_index.add_argument("--reindex", action="store_true", help="Force re-parse all sessions.")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search sessions (interactive picker by default).")
    p_search.add_argument("query", nargs="?", help="FTS query (use --all or '*' to browse).")
    p_search.add_argument("--codex-dir", type=Path, default=default_codex_dir)
    p_search.add_argument("--db", type=Path, default=default_db)
    p_search.add_argument("--limit", type=int, default=200)
    p_search.add_argument("--all", action="store_true", help="Browse most recent sessions (ignore query).")
    p_search.add_argument("--no-index", action="store_true", help="Skip auto indexing.")
    p_search.add_argument("--reindex", action="store_true", help="Force re-parse all sessions before searching.")
    p_search.add_argument("--no-ui", action="store_true", help="Print results, no interactive picker.")
    p_search.add_argument("--copy", action="store_true", help="Copy selected id to clipboard (macOS pbcopy).")
    p_search.set_defaults(func=cmd_search)

    p_live = sub.add_parser("live", help="Interactive live search: type/edit query and see results update.")
    p_live.add_argument("query", nargs="?", help="Initial query (optional).")
    p_live.add_argument("--codex-dir", type=Path, default=default_codex_dir)
    p_live.add_argument("--db", type=Path, default=default_db)
    p_live.add_argument("--limit", type=int, default=200)
    p_live.add_argument("--no-index", action="store_true", help="Skip auto indexing.")
    p_live.add_argument("--reindex", action="store_true", help="Force re-parse all sessions before opening UI.")
    p_live.add_argument("--copy", action="store_true", help="Copy selected id to clipboard (macOS pbcopy).")
    p_live.set_defaults(func=cmd_live)

    p_export = sub.add_parser("export", help="Export a session to Markdown (optionally redacted).")
    p_export.add_argument("session_id")
    p_export.add_argument("--db", type=Path, default=default_db)
    p_export.add_argument("--out", help="Output path (defaults to stdout).")
    p_export.add_argument("--redact", action="store_true", help="Best-effort redaction of obvious secrets/paths.")
    p_export.set_defaults(func=cmd_export)

    p_fork = sub.add_parser("fork", help="Fork a session into a new Codex session (private full context).")
    p_fork.add_argument("session_id")
    p_fork.add_argument("--codex-dir", type=Path, default=default_codex_dir)
    p_fork.add_argument("--db", type=Path, default=default_db)
    p_fork.add_argument("--out-dir", default=str(Path.home() / ".codex-user" / "forks"))
    p_fork.add_argument("--max-chars", type=int, default=200_000, help="Max transcript chars to include in prompt.")
    p_fork.add_argument("--cd", action="store_true", help="If session cwd exists, start Codex in that directory.")
    p_fork.add_argument("--no-index", action="store_true")
    p_fork.add_argument("--reindex", action="store_true")
    p_fork.add_argument("user_prompt", nargs="?", help="Optional prompt to start the fork.")
    p_fork.set_defaults(func=cmd_fork)

    p_share = sub.add_parser("share", help="Share a session pack (local file or private Gist).")
    p_share.add_argument("session_id")
    p_share.add_argument("--codex-dir", type=Path, default=default_codex_dir)
    p_share.add_argument("--db", type=Path, default=default_db)
    p_share.add_argument("--out-dir", default=str(Path.home() / ".codex-user" / "shares"))
    p_share.add_argument("--method", choices=["file", "gist"], default="file")
    p_share.add_argument("--title", help="Gist description/title.")
    p_share.add_argument("--no-redact", action="store_true", help="Disable redaction (NOT recommended for sharing).")
    p_share.add_argument("--no-index", action="store_true")
    p_share.add_argument("--reindex", action="store_true")
    p_share.set_defaults(func=cmd_share)

    p_import = sub.add_parser("import", help="Import a session pack and start a new Codex session with that context.")
    p_import.add_argument("path")
    p_import.add_argument("--max-chars", type=int, default=200_000)
    p_import.add_argument("--cd", action="store_true", help="If pack has a cwd that exists, start Codex in that directory.")
    p_import.add_argument("user_prompt", nargs="?", help="Optional prompt to start after import.")
    p_import.set_defaults(func=cmd_import)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
