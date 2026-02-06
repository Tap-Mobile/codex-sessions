#!/usr/bin/env bash
set -euo pipefail

say() {
  printf "%s\n" "$*"
}

remove_block() {
  local rc_file="$1"
  local start="# >>> codex-sessions resumev2 >>>"
  local end="# <<< codex-sessions resumev2 <<<"

  if [[ ! -f "$rc_file" ]]; then
    return 0
  fi

  python3 - "$rc_file" "$start" "$end" <<'PY'
from pathlib import Path
import sys

rc_file = sys.argv[1]
start = sys.argv[2]
end = sys.argv[3]

p = Path(rc_file).expanduser()
text = p.read_text(encoding="utf-8", errors="replace")
if start not in text or end not in text:
    raise SystemExit(0)

pre, rest = text.split(start, 1)
_mid, post = rest.split(end, 1)
out = pre.rstrip() + "\n\n" + post.lstrip()
p.write_text(out, encoding="utf-8")
PY
}

main() {
  remove_block "$HOME/.zshrc"
  remove_block "$HOME/.bashrc"

  rm -f "$HOME/.local/bin/codex_sessions" \
    "$HOME/.local/bin/codex_sessions_index" \
    "$HOME/.local/bin/codex_sessions_search" 2>/dev/null || true

  say "Removed resumev2 shell wrapper (if present)."
  say "Note: kept $HOME/.codex-user/codex_sessions.py and DB (you can delete manually)."
}

main "$@"
