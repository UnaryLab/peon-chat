#!/bin/sh
# Rotate peon-chat.log when it exceeds 10 MB: gzip a copy, truncate in place
# (launchd holds an O_APPEND fd, so rename-based rotation would not work), keep 5 archives.
LOG=/Users/diwu/Projects/peon-chat/peon-chat.log
[ -f "$LOG" ] || exit 0
[ "$(stat -f%z "$LOG")" -gt 10485760 ] || exit 0
gzip -c "$LOG" > "$LOG.$(date +%Y%m%d-%H%M%S).gz"
: > "$LOG"
# ponytail: lines written during the gzip copy are lost at truncate; fine for logs
ls -t "$LOG".*.gz 2>/dev/null | tail -n +6 | xargs rm -f
