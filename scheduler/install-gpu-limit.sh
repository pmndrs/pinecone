#!/bin/zsh
# Lets the scheduler set the GPU memory limit (iogpu.wired_limit_mb) without a password, and removes the
# boot-time setting from /etc/sysctl.conf, because the scheduler now sets it when it starts and puts the
# macOS default back when it stops. The menu bar app runs this for you (Settings, Allow); by hand:
#   sudo ./install-gpu-limit.sh      Undo:  sudo ./install-gpu-limit.sh remove
set -e
[ "$EUID" -eq 0 ] || { echo "Run this with sudo."; exit 1 }
HELPER=/usr/local/sbin/pinecone-gpu-limit
RULE=/etc/sudoers.d/pinecone-gpu-limit
if [ "$1" = remove ]; then
  rm -f "$HELPER" "$RULE"
  /usr/sbin/sysctl iogpu.wired_limit_mb=0 >/dev/null
  echo "Removed. The GPU memory limit is back to the macOS default."
  exit 0
fi
WHO=${SUDO_USER:-$1}   # the menu bar app passes the account name, sudo sets SUDO_USER
[ -n "$WHO" ] && id "$WHO" >/dev/null 2>&1 || { echo "Run this with sudo from your own account."; exit 1 }
mkdir -p /usr/local/sbin
chown root:wheel /usr/local/sbin
cat > "$HELPER" <<'SH'
#!/bin/sh
# Sets how much memory the GPU may wire, in megabytes. 0 puts the macOS default back.
case "$1" in ''|*[!0-9]*) echo "usage: pinecone-gpu-limit <megabytes, or 0 for the default>" >&2; exit 2;; esac
exec /usr/sbin/sysctl iogpu.wired_limit_mb="$1"
SH
chown root:wheel "$HELPER"
chmod 755 "$HELPER"
TMP=$(mktemp)
echo "$WHO ALL=(root) NOPASSWD: $HELPER" > "$TMP"
/usr/sbin/visudo -cf "$TMP" >/dev/null
install -o root -g wheel -m 440 "$TMP" "$RULE"
rm -f "$TMP"
if [ -f /etc/sysctl.conf ] && grep -q '^iogpu\.wired_limit_mb' /etc/sysctl.conf; then
  cp /etc/sysctl.conf /etc/sysctl.conf.before-pinecone
  grep -v '^iogpu\.wired_limit_mb' /etc/sysctl.conf > /etc/sysctl.conf.new || true
  if [ -s /etc/sysctl.conf.new ]; then mv /etc/sysctl.conf.new /etc/sysctl.conf; else rm -f /etc/sysctl.conf.new /etc/sysctl.conf; fi
  echo "Removed the boot-time iogpu.wired_limit_mb line from /etc/sysctl.conf (copy kept as /etc/sysctl.conf.before-pinecone)."
fi
echo "Installed. The scheduler picks this up within half a minute."
