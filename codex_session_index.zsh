codex_tag() {
  local key="${1:-}"
  if [[ -z "$key" || "$key" == "-h" || "$key" == "--help" ]]; then
    cat <<'EOF'
usage:
  codex_tag <key> [tags] [note...]

examples:
  codex_tag "resume:billing-webhook" "#stripe #prod ticket:JIRA-123" "what I tried + next steps"
  codex_tag "resume:perf-investigation" "#cuda #trt" "benchmark commands and results"

search:
  rg -n "resume:billing-webhook|#stripe|JIRA-123" ~/.codex-user/session-index.log
EOF
    return 2
  fi
  shift

  local tags="${1:-}"
  if [[ $# -gt 0 ]]; then shift; fi
  local note="$*"

  local ts
  ts="$(date '+%Y-%m-%d %H:%M')"

  local dir="$PWD"
  local git_branch=""
  local git_sha=""
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    git_sha="$(git rev-parse --short HEAD 2>/dev/null || true)"
  fi

  mkdir -p "$HOME/.codex-user"
  printf "%s | %s | %s | dir:%s | git:%s@%s | %s\n" \
    "$ts" "$key" "$tags" "$dir" "${git_branch:-n/a}" "${git_sha:-n/a}" "${note:-}" \
    >> "$HOME/.codex-user/session-index.log"
}

codex_find() {
  if ! command -v rg >/dev/null 2>&1; then
    echo "codex_find requires ripgrep (rg)."
    return 127
  fi
  if [[ $# -lt 1 ]]; then
    echo "usage: codex_find <pattern>"
    return 2
  fi
  rg -n --hidden --no-ignore-vcs -- "$*" "$HOME/.codex-user/session-index.log"
}

codex_sessions_index() {
  python3 "$HOME/.codex-user/codex_sessions.py" index "$@"
}

codex_sessions_search() {
  python3 "$HOME/.codex-user/codex_sessions.py" search "$@"
}

codex_sessions() {
  # Convenience wrapper: live search UI (type/edit query, results update).
  python3 "$HOME/.codex-user/codex_sessions.py" live "$@"
}
