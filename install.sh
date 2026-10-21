#!/bin/sh
# nodeseek-vps-watch 安装脚本
#
# 把 watch.py 装到不受 macOS TCC 限制的运行时目录，并注册定时任务。
# macOS 用 LaunchAgent，Linux 用 crontab。
set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
LABEL="com.nodeseek.vpswatch"
DIGEST_LABEL="com.nodeseek.vpswatch.digest"
INTERVAL=60
DIGEST_TIME="09:00"
TARGET_DIR=""
PYTHON_BIN=""
UNINSTALL=0

usage() {
  cat <<'EOF'
用法: ./install.sh [选项]

  --dir <路径>       运行时目录（默认 macOS: ~/Library/Application Support/NodeSeekVPSWatch，
                     Linux: ~/.local/share/nodeseek-vps-watch）
  --interval <秒>    轮询间隔秒数，默认 60
  --daily <HH:MM>    每天几点发汇总邮件，默认 09:00
  --no-daily         不安装每日汇总任务
  --python <路径>    指定 python3 解释器
  --uninstall        卸载定时任务（保留配置和数据）
  -h, --help         显示帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) TARGET_DIR="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --daily) DIGEST_TIME="$2"; shift 2 ;;
    --no-daily) DIGEST_TIME=""; shift ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

OS=$(uname -s)
if [ -z "$TARGET_DIR" ]; then
  if [ "$OS" = "Darwin" ]; then
    TARGET_DIR="$HOME/Library/Application Support/NodeSeekVPSWatch"
  else
    TARGET_DIR="$HOME/.local/share/nodeseek-vps-watch"
  fi
fi
case "$TARGET_DIR" in
  /*) ;;
  *) TARGET_DIR="$(cd "$(dirname "$TARGET_DIR")" && pwd)/$(basename "$TARGET_DIR")" ;;
esac

if [ -z "$PYTHON_BIN" ]; then
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$candidate" ]; then PYTHON_BIN="$candidate"; break; fi
  done
  [ -n "$PYTHON_BIN" ] || PYTHON_BIN=$(command -v python3 || true)
fi

remove_cron_block() {
  if command -v crontab >/dev/null 2>&1; then
    tmp=$(mktemp)
    crontab -l 2>/dev/null | grep -v 'nodeseek-vps-watch' > "$tmp" || true
    crontab "$tmp" 2>/dev/null || true
    rm -f "$tmp"
  fi
}

uninstall() {
  echo "卸载定时任务（保留 $TARGET_DIR 下的配置与数据）"
  if [ "$OS" = "Darwin" ]; then
    for label in "$LABEL" "$DIGEST_LABEL"; do
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
      rm -f "$HOME/Library/LaunchAgents/$label.plist"
    done
  fi
  remove_cron_block
  echo "完成。"
}

if [ "$UNINSTALL" = "1" ]; then
  uninstall
  exit 0
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "没有找到 python3，请用 --python 指定解释器路径" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR/logs"
cp "$SRC_DIR/watch.py" "$TARGET_DIR/watch.py"
if [ ! -f "$TARGET_DIR/config.json" ]; then
  cp "$SRC_DIR/config.example.json" "$TARGET_DIR/config.json"
  chmod 600 "$TARGET_DIR/config.json"
  echo "已生成配置文件: $TARGET_DIR/config.json（记得填 SMTP）"
fi
[ -f "$SRC_DIR/README.md" ] && cp "$SRC_DIR/README.md" "$TARGET_DIR/README.md"

cat > "$TARGET_DIR/run.sh" <<EOF
#!/bin/sh
cd "$TARGET_DIR" || exit 1
exec "$PYTHON_BIN" watch.py run
EOF
chmod +x "$TARGET_DIR/run.sh"

if [ -n "$DIGEST_TIME" ]; then
  cat > "$TARGET_DIR/digest.sh" <<EOF
#!/bin/sh
cd "$TARGET_DIR" || exit 1
exec "$PYTHON_BIN" watch.py digest
EOF
  chmod +x "$TARGET_DIR/digest.sh"
fi

echo "运行目录: $TARGET_DIR"
echo "解释器:   $PYTHON_BIN"

if [ "$OS" = "Darwin" ]; then
  AGENTS="$HOME/Library/LaunchAgents"
  mkdir -p "$AGENTS"

  cat > "$AGENTS/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>$TARGET_DIR/run.sh</string></array>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$TARGET_DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$TARGET_DIR/logs/launchd.err.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$LABEL.plist"
  echo "已加载 LaunchAgent: ${LABEL}（每 ${INTERVAL} 秒轮询一次）"

  if [ -n "$DIGEST_TIME" ]; then
    HOUR=$(printf '%d' "${DIGEST_TIME%%:*}" 2>/dev/null || echo 9)
    MINUTE=$(printf '%d' "${DIGEST_TIME##*:}" 2>/dev/null || echo 0)
    cat > "$AGENTS/$DIGEST_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$DIGEST_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>$TARGET_DIR/digest.sh</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$TARGET_DIR/logs/digest.out.log</string>
  <key>StandardErrorPath</key><string>$TARGET_DIR/logs/digest.err.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$DIGEST_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$DIGEST_LABEL.plist"
    echo "已加载每日汇总任务: 每天 ${DIGEST_TIME} 发邮件"
  fi
else
  remove_cron_block
  if [ "$INTERVAL" -ge 60 ]; then
    MINSPEC="0"
  else
    MINSPEC="*/$INTERVAL"
  fi
  CRON_LINES="$MINSPEC * * * * cd \"$TARGET_DIR\" && \"$PYTHON_BIN\" watch.py run >/dev/null 2>&1 # nodeseek-vps-watch"
  if [ -n "$DIGEST_TIME" ]; then
    CHOUR=$(printf '%d' "${DIGEST_TIME%%:*}" 2>/dev/null || echo 9)
    CMIN=$(printf '%d' "${DIGEST_TIME##*:}" 2>/dev/null || echo 0)
    CRON_LINES="$CRON_LINES
$CMIN $CHOUR * * * cd \"$TARGET_DIR\" && \"$PYTHON_BIN\" watch.py digest >/dev/null 2>&1 # nodeseek-vps-watch-digest"
  fi
  if command -v crontab >/dev/null 2>&1; then
    tmp=$(mktemp)
    crontab -l 2>/dev/null | grep -v 'nodeseek-vps-watch' > "$tmp" || true
    printf '%s\n' "$CRON_LINES" >> "$tmp"
    crontab "$tmp"
    rm -f "$tmp"
    echo "已写入 crontab 定时任务"
  else
    echo "未找到 crontab，请手动添加以下任务："
    echo "$CRON_LINES"
  fi
fi

echo
echo "下一步:"
echo "  1) 编辑 $TARGET_DIR/config.json，把 smtp.enabled 改成 true 并填入邮箱与授权码"
echo "  2) 测试邮件: $PYTHON_BIN \"$TARGET_DIR/watch.py\" test-email"
echo "  3) 查看状态: $PYTHON_BIN \"$TARGET_DIR/watch.py\" status"
