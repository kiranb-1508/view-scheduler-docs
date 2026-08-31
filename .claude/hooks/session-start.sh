#!/bin/bash
# Sets up the Databricks CLI and its agent skills for Claude Code on the web.
# The container is rebuilt for every remote session, so this has to run each time.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

if ! command -v databricks >/dev/null 2>&1; then
  echo "Installing Databricks CLI..."
  curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
fi

databricks --version

# Skills land in .databricks/aitools/skills with symlinks from .claude/skills.
# Both are gitignored, so they are re-fetched (and stay current) each session.
if [ ! -d "$CLAUDE_PROJECT_DIR/.claude/skills/databricks-core" ]; then
  echo "Installing Databricks agent skills..."
  databricks aitools install --skills-only --agents claude-code --scope project
fi

# DATABRICKS_HOST / DATABRICKS_TOKEN come from the environment's variable
# settings; warn rather than fail if they are missing.
if [ -z "${DATABRICKS_HOST:-}" ] || [ -z "${DATABRICKS_TOKEN:-}" ]; then
  echo "WARNING: DATABRICKS_HOST/DATABRICKS_TOKEN not set - CLI will not authenticate."
fi
