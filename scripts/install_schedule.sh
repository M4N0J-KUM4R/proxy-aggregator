#!/bin/bash
# install_schedule.sh — install (or remove) the launchd job that refreshes the
# proxy pool.
#
#   ./install_schedule.sh install [hour] [minute]
#   ./install_schedule.sh uninstall
#   ./install_schedule.sh status
#
# The job runs the whole pipeline: check in the upstream repositories, scrape,
# validate, categorize, merge into the router's pool, publish into the 9router
# repository and reload the running shim.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.local.proxy-pool"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$DIR/work"

cmd="${1:-install}"
hour="${2:-6}"
minute="${3:-41}"

case "$cmd" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$DIR/scripts/refresh_and_publish.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>$hour</integer>
        <key>Minute</key><integer>$minute</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/launchd.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
PLIST_EOF
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
    echo "installed $LABEL at $(printf '%02d:%02d' "$hour" "$minute") daily"
    echo "  the run takes 8-12 minutes: 34 repository check-ins, a scrape of"
    echo "  105 lists, then the validation of every proxy in them."
    launchctl list | grep "$LABEL" || true
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "removed $LABEL"
    ;;
  status)
    launchctl list | grep "$LABEL" || echo "$LABEL is not loaded"
    echo
    echo "log: $LOG_DIR/refresh.log"
    tail -5 "$LOG_DIR/refresh.log" 2>/dev/null
    ;;
  run)
    # run it now, in the foreground, the way launchd would
    exec bash "$DIR/scripts/refresh_and_publish.sh"
    ;;
  *)
    echo "usage: $0 install|uninstall|status|run [hour] [minute]" >&2
    exit 2
    ;;
esac
