#!/usr/bin/env bash
# 把本技能装到 agent 的技能目录。
#
# 安全约定（v2 起）：
#   * 不再用 rsync --delete —— 旧版本会清空目标目录，误指到 $HOME 就出事
#   * 只允许装进已知的技能目录（见 ALLOWED_ROOTS），或已经是本技能安装点的目录
#   * 目标已存在且有别人的文件时，默认不动；--force 也只做「备份后重装」，绝不删除
#
# 用法：
#   ./install.sh                      # 装到 ~/.workbuddy/skills/srt-proofread-prproj
#   ./install.sh ~/.claude/skills/srt-proofread-prproj
#   ./install.sh --force <目标目录>    # 目标非空且不是本技能时，备份后安装
set -euo pipefail

MARKER=".srt-proofread-installed"
SKILL_NAME="srt-proofread-prproj"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SRC/VERSION" 2>/dev/null | tr -d '[:space:]')"
VERSION="${VERSION:-unknown}"

FORCE=0
if [ "${1:-}" = "--force" ]; then
  FORCE=1
  shift
fi
DEST="${1:-$HOME/.workbuddy/skills/$SKILL_NAME}"

die() { printf '安装已中止：%s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 1. 校验目标
case "$DEST" in
  /*) : ;;
  *) die "目标必须是绝对路径（收到：${DEST}）" ;;
esac
case "$DEST" in
  *..*) die "目标路径不许包含 '..'（收到：${DEST}）" ;;
esac
[ "$DEST" != "/" ] || die "不能装到根目录"
DEST="${DEST%/}"
[ -n "$DEST" ] || die "目标路径为空"
[ "$DEST" != "$HOME" ] || die "不能装到家目录本身"

# 已知的技能安装根目录（白名单）
ALLOWED_ROOTS=(
  "$HOME/.workbuddy/skills"
  "$HOME/.claude/skills"
  "$HOME/.config/agent/skills"
)

DEST_PARENT="$(cd "$(dirname "$DEST")" 2>/dev/null && pwd -P || true)"
IN_ALLOWED=0
if [ -n "$DEST_PARENT" ]; then
  for root in "${ALLOWED_ROOTS[@]}"; do
    case "$DEST_PARENT/" in
      "$root"/*) IN_ALLOWED=1 ;;
    esac
  done
fi

# 已经装过的目录（有标记文件）也允许升级，即使不在白名单里
IS_OURS=0
[ -f "$DEST/$MARKER" ] && IS_OURS=1

if [ "$IN_ALLOWED" -eq 0 ] && [ "$IS_OURS" -eq 0 ]; then
  cat >&2 <<EOF
安装已中止：目标不在允许的技能目录里。

  目标：$DEST

允许的位置（任选其一）：
  $HOME/.workbuddy/skills/$SKILL_NAME
  $HOME/.claude/skills/$SKILL_NAME

如果确实要装到别处，请手动建好那个目录，并确认它是 agent 会读取的技能目录。
EOF
  exit 1
fi

# ---------------------------------------------------------------- 2. 检查源
for item in SKILL.md VERSION scripts references examples; do
  [ -e "$SRC/$item" ] || die "源目录缺少 ${item}，仓库可能不完整"
done
for py in "$SRC"/scripts/*.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$py" \
    || die "脚本语法检查不通过：$py"
done

# ---------------------------------------------------------------- 3. 处理已存在的目标
if [ -d "$DEST" ]; then
  EXISTING="$(ls -A "$DEST" 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$EXISTING" != "0" ] && [ "$IS_OURS" -eq 0 ]; then
    if [ "$FORCE" -ne 1 ]; then
      cat >&2 <<EOF
安装已中止：$DEST 已存在且非空，也不是本技能的安装点。

为了不碰你别的文件，这里不会动手。可选：
  1) 换一个目标目录（推荐）
  2) 先自己确认里面是什么，挪走再装
  3) 加 --force：先把整个目录改名为 .bak.<时间戳> 备份，再装新的（不删除任何东西）
EOF
      exit 1
    fi
    BACKUP="$DEST.bak.$(date +%Y%m%d%H%M%S)"
    mv "$DEST" "$BACKUP"
    echo "已备份原目录 → ${BACKUP}（没有删除任何文件）"
  fi
fi

# ---------------------------------------------------------------- 4. 拷贝（只写，不删）
mkdir -p "$DEST"
for f in SKILL.md README.md LICENSE VERSION CHANGELOG.md; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DEST/"
done
cp -R "$SRC/scripts" "$SRC/references" "$SRC/examples" "$DEST/"
# 清掉可能被拷过来的字节码，避免和本机 Python 版本冲突（只删自己拷来的 pyc）
find "$DEST/scripts" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST/scripts" -name '*.pyc' -delete 2>/dev/null || true

chmod +x "$DEST/scripts/"*.py 2>/dev/null || true

cat > "$DEST/$MARKER" <<EOF
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
version=$VERSION
source=$SRC
EOF

cat <<EOF
installed → $DEST

注意：安装是「覆盖式拷贝」，不会删除目标里原有的其他文件。
      要彻底重装，请先手动移走旧目录，或加 --force（会先备份整目录）。

试一下：
  python3 "$DEST/scripts/proofread.py" "$DEST/examples/demo.srt" --report /tmp/demo-report.md
EOF
