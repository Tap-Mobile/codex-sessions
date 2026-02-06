#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_BASE="https://raw.githubusercontent.com/Tap-Mobile/codex-sessions/main"

say() {
  printf "%s\n" "$*"
}

die() {
  say "error: $*"
  exit 1
}

fetch() {
  local url="$1"
  local out="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$out"
    return 0
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "$out" "$url"
    return 0
  fi
  die "need curl or wget to download files"
}

require_python() {
  if command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  die "python3 not found on PATH"
}

check_fts5() {
  # Fail early if python sqlite3 doesn't support FTS5.
  python3 - <<'PY' >/dev/null
import sqlite3
con = sqlite3.connect(":memory:")
con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
con.close()
PY
}

append_block_if_missing() {
  local rc_file="$1"
  local start="$2"
  local end="$3"
  local block="$4"

  mkdir -p "$(dirname "$rc_file")"
  touch "$rc_file"

  if grep -qF "$start" "$rc_file"; then
    return 0
  fi

  {
    printf "\n%s\n" "$start"
    printf "%s\n" "$block"
    printf "%s\n" "$end"
  } >>"$rc_file"
}

main() {
  require_python
  check_fts5 || die "python3 sqlite3 missing FTS5; install a python build with SQLite FTS5 enabled"

  local codex_user_dir="$HOME/.codex-user"
  local local_bin_dir="$HOME/.local/bin"
  mkdir -p "$codex_user_dir" "$local_bin_dir"

  # Install python script.
  local tmp
  tmp="$(mktemp)"
  fetch "$REPO_RAW_BASE/codex_sessions.py" "$tmp"
  mv "$tmp" "$codex_user_dir/codex_sessions.py"
  chmod +x "$codex_user_dir/codex_sessions.py"

  # Install small wrapper scripts (optional but nice to have).
  fetch "$REPO_RAW_BASE/bin/codex_sessions" "$local_bin_dir/codex_sessions"
  fetch "$REPO_RAW_BASE/bin/codex_sessions_index" "$local_bin_dir/codex_sessions_index"
  fetch "$REPO_RAW_BASE/bin/codex_sessions_search" "$local_bin_dir/codex_sessions_search"
  chmod +x "$local_bin_dir/codex_sessions" "$local_bin_dir/codex_sessions_index" "$local_bin_dir/codex_sessions_search"

  # Pick a shell rc file. Only zsh + bash supported for automatic install.
  local shell_name
  shell_name="$(basename "${SHELL:-}")"

  local rc=""
  case "$shell_name" in
    zsh) rc="$HOME/.zshrc" ;;
    bash) rc="$HOME/.bashrc" ;;
  esac

  if [[ -z "$rc" ]]; then
    say "Installed:"
    say "  - $codex_user_dir/codex_sessions.py"
    say "  - $local_bin_dir/codex_sessions{,_index,_search}"
    say ""
    say "Shell auto-setup supports zsh/bash only."
    say "Manual setup: add this to your shell rc:"
    say ""
    cat <<'EOF'
# >>> codex-sessions resumev2 >>>
export PATH="$HOME/.local/bin:$PATH"
codex() {
  if [[ "$1" == "resumev2" ]]; then
    shift
    local pass=() q=()
    while [[ "$#" -gt 0 ]]; do
      case "$1" in
        --codex-dir|--db|--limit)
          pass+=("$1"); shift
          if [[ "$#" -gt 0 ]]; then pass+=("$1"); shift; fi
          ;;
        -h|--help|--no-index|--reindex|--copy)
          pass+=("$1"); shift
          ;;
        --)
          shift
          q+=("$@")
          break
          ;;
        -*)
          pass+=("$1"); shift
          ;;
        *)
          q+=("$1"); shift
          ;;
      esac
    done
    if [[ "${#q[@]}" -gt 0 ]]; then
      pass+=("${q[*]}")
    fi
    if command -v codex_sessions >/dev/null 2>&1; then
      codex_sessions "${pass[@]}"
      return $?
    fi
    python3 "$HOME/.codex-user/codex_sessions.py" live "${pass[@]}"
    return $?
  fi
  command codex "$@"
}
# <<< codex-sessions resumev2 <<<
EOF
    exit 0
  fi

  local start="# >>> codex-sessions resumev2 >>>"
  local end="# <<< codex-sessions resumev2 <<<"
  local block
  block="$(cat <<'EOF'
# Ensure ~/.local/bin is on PATH for wrapper scripts (codex_sessions).
export PATH="$HOME/.local/bin:$PATH"

# Adds a "virtual" Codex subcommand: `codex resumev2 [query...]`
# Everything else passes through to the real Codex CLI unchanged.
codex() {
  if [[ "$1" == "resumev2" ]]; then
    shift
    # `codex_sessions.py live` accepts only one positional `query`, so we join
    # extra words while preserving known flags.
    local pass=() q=()
    while [[ "$#" -gt 0 ]]; do
      case "$1" in
        --codex-dir|--db|--limit)
          pass+=("$1")
          shift
          if [[ "$#" -gt 0 ]]; then
            pass+=("$1")
            shift
          fi
          ;;
        -h|--help|--no-index|--reindex|--copy)
          pass+=("$1")
          shift
          ;;
        --)
          shift
          q+=("$@")
          break
          ;;
        -*)
          # Unknown option: pass through.
          pass+=("$1")
          shift
          ;;
        *)
          q+=("$1")
          shift
          ;;
      esac
    done
    if [[ "${#q[@]}" -gt 0 ]]; then
      pass+=("${q[*]}")
    fi

    if command -v codex_sessions >/dev/null 2>&1; then
      codex_sessions "${pass[@]}"
      return $?
    fi
    python3 "$HOME/.codex-user/codex_sessions.py" live "${pass[@]}"
    return $?
  fi

  command codex "$@"
}
EOF
)"

  append_block_if_missing "$rc" "$start" "$end" "$block"

  say "Installed resumev2 into $rc"
  say "Next:"
  say "  1) Restart your shell, or run: source \"$rc\""
  say "  2) Run: codex resumev2"
}

main "$@"

