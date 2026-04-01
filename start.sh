#!/bin/bash
set -euo pipefail

# Copy config to OpenClaw directory
mkdir -p ~/.openclaw
cp /app/openclaw.config.json ~/.openclaw/config.json
cp /app/SOUL.md ~/.openclaw/SOUL.md

# Start OpenClaw gateway (handles Telegram + calls MCP server)
exec openclaw gateway start
