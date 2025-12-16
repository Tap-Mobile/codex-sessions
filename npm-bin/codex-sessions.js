#!/usr/bin/env node
/* eslint-disable no-console */
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function usage(exitCode) {
  const msg = `
codex-sessions

Usage:
  npx codex-sessions [query...]
  npx codex-sessions --help

Behavior:
  - Runs the Python TUI: codex_sessions.py live [query...]
  - Uses your local Codex sessions at ~/.codex/sessions/
  - Stores the local index DB at ~/.codex-user/codex_sessions.db

Notes:
  - Requires python3 with SQLite FTS5 enabled (macOS system python or Homebrew python works).
  - "codex resume" must be available on PATH to resume sessions from the UI.
`;
  console.error(msg.trim() + "\n");
  process.exit(exitCode);
}

function which(cmd) {
  const res = spawnSync(process.platform === "win32" ? "where" : "command", process.platform === "win32" ? [cmd] : ["-v", cmd], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (res.status !== 0) return null;
  const out = (res.stdout || "").trim().split("\n")[0];
  return out || null;
}

function run() {
  const argv = process.argv.slice(2);
  if (argv.includes("-h") || argv.includes("--help")) {
    usage(0);
  }

  const python = which("python3") || which("python");
  if (!python) {
    console.error("codex-sessions: python3 not found on PATH.");
    console.error("Install Python 3, then retry.");
    process.exit(2);
  }

  const pyPath = path.resolve(__dirname, "..", "codex_sessions.py");
  if (!fs.existsSync(pyPath)) {
    console.error(`codex-sessions: missing bundled python script at ${pyPath}`);
    process.exit(2);
  }

  // Ensure local storage exists.
  const codexUserDir = path.join(os.homedir(), ".codex-user");
  try {
    fs.mkdirSync(codexUserDir, { recursive: true });
  } catch (e) {
    console.error(`codex-sessions: failed to create ${codexUserDir}: ${e.message || e}`);
    process.exit(2);
  }

  const args = [pyPath, "live", ...argv];
  const res = spawnSync(python, args, { stdio: "inherit" });
  process.exit(res.status ?? 1);
}

run();

