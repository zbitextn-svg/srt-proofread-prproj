#!/usr/bin/env bash
# 把本技能装到 agent 的技能目录（默认 WorkBuddy）
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/.workbuddy/skills/srt-proofread-prproj}"
mkdir -p "$DEST"
rsync -a --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC/" "$DEST/"
chmod +x "$DEST/scripts/"*.py 2>/dev/null || true
echo "installed → $DEST"
echo "试试： python3 $DEST/scripts/proofread.py $DEST/examples/demo.srt"
