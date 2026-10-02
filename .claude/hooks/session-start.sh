#!/bin/bash
# Installs the Garmin CLI (garmin-pp-cli) and its pp-garmin skill in Claude Code cloud sessions.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

BIN_DIR="$HOME/.local/bin"

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

if [ -x "$BIN_DIR/garmin-pp-cli" ] && [ -d "$HOME/.claude/skills/pp-garmin" ]; then
  exit 0
fi

# The session's GitHub tokens are scoped to this repo only; sending them to the
# public printing-press catalog makes raw.githubusercontent.com return 404.
env -u GH_TOKEN -u GITHUB_TOKEN npx -y @mvanhorn/printing-press-library install garmin
