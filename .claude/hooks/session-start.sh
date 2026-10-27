#!/bin/bash
set -euo pipefail

# Only run in Claude Code cloud sessions
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

export PATH="$HOME/.local/bin:$PATH"

# Install uv if missing
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Install Agent Reach from the GitHub release (NOT the unrelated PyPI package)
if ! command -v agent-reach >/dev/null 2>&1; then
  uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip >&2
fi

# Enable default channels and install the agent skill (idempotent)
agent-reach setup >/dev/null

# Make agent-reach available on PATH for the session
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$CLAUDE_ENV_FILE"
fi
