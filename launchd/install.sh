#!/bin/zsh
# Runs the scheduler and the media server in the background, now and at every login (no sudo needed).
#   launchd/install.sh            install and start both      launchd/install.sh scheduler    only the scheduler
#   launchd/install.sh remove     stop and remove both
set -e
ROOT=${0:A:h:h}
AGENTS=~/Library/LaunchAgents
WHAT=(scheduler media-mcp)
[ "$1" = scheduler ] && WHAT=(scheduler)
HOME_DIR=${PINECONE_HOME:-$HOME/.pinecone}   # the user's folder: config, key, logs
mkdir -p "$AGENTS" "$HOME_DIR/logs"
for name in $WHAT; do
  label=dev.pinecone.$name
  launchctl bootout gui/$UID/$label 2>/dev/null || true
  if [ "$1" = remove ]; then rm -f "$AGENTS/$label.plist"; echo "removed $label"; continue; fi
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME_DIR|g" "$ROOT/launchd/$label.plist" > "$AGENTS/$label.plist"
  launchctl bootstrap gui/$UID "$AGENTS/$label.plist"
  echo "started $label"
done
