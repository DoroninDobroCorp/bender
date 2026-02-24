#!/bin/bash
# Emergency Bender cleanup - запускать когда PTY исчерпались!
# Usage: ./scripts/bender_emergency_cleanup.sh

echo "🚨 Emergency Bender Cleanup"
echo "=========================="

# 1. Kill bender-run-* scripts
echo ""
echo "1. Killing bender-run-* scripts..."
pgrep -f "bender-run-" | while read pid; do
    echo "   Killing PID $pid"
    kill -9 $pid 2>/dev/null
done

# 2. Kill bender-inner-* scripts  
echo ""
echo "2. Killing bender-inner-* scripts..."
pgrep -f "bender-inner-" | while read pid; do
    echo "   Killing PID $pid"
    kill -9 $pid 2>/dev/null
done

# 3. Kill script processes holding bender sessions
echo ""
echo "3. Killing script processes (PTY holders)..."
ps -eo pid,command | grep -i 'script.*bender' | grep -v grep | while read line; do
    pid=$(echo "$line" | awk '{print $1}')
    if [ -n "$pid" ] && [ "$pid" != "PID" ]; then
        echo "   Killing PID $pid: $line"
        kill -9 $pid 2>/dev/null
    fi
done

# 4. Kill tmux bender sessions
echo ""
echo "4. Killing bender tmux sessions..."
tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^bender-' | while read session; do
    echo "   Killing session: $session"
    tmux kill-session -t "$session" 2>/dev/null
done

# 5. Close Terminal windows with BENDER in title (macOS only)
if [ "$(uname)" = "Darwin" ]; then
    echo ""
    echo "5. Closing BENDER Terminal windows (macOS)..."
    osascript -e '
    tell application "Terminal"
        repeat with w in windows
            try
                set wName to name of w
                if wName contains "BENDER" or wName contains "bender" then
                    close w saving no
                end if
            end try
        end repeat
    end tell
    ' 2>/dev/null
fi

# 6. Clean temp files
echo ""
echo "6. Cleaning temp files..."
rm -f /tmp/bender-*.log /tmp/bender-*.done /tmp/bender-*.txt /tmp/bender-*.sh 2>/dev/null
echo "   Cleaned /tmp/bender-* files"

echo ""
echo "✅ Emergency cleanup complete!"
echo ""
echo "Если PTY всё ещё не работает:"
echo "  1. Закрой все Terminal окна"
echo "  2. Открой новый Terminal"
echo "  3. Если не помогло - перезагрузи Mac"
