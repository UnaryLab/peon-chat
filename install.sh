#!/usr/bin/env bash
# One-step installer for peon-chat: conda env, dependencies, the peon-chat command, .env, Slack manifests.
# Pass --service to also install and load the always-on service unit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

SERVICE=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --service) SERVICE=1 ;;
    --force) FORCE=1 ;;
    -h|--help)
      echo "usage: ./install.sh [--service] [--force]"
      echo "  --service  also install and load the launchd/systemd unit"
      echo "  --force    with --service, replace an existing unit file"
      exit 0 ;;
    *) echo "install.sh: unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Refuse to replace an installed unit unless --force, then make sure its dir exists.
guard_dest() {
  if [ "$FORCE" = 0 ] && [ -f "$1" ]; then
    echo "install.sh: $1 already exists; pass --force to replace it." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$1")"
}

# 1. Preflight.
if ! command -v conda >/dev/null; then
  echo "install.sh: missing required program: conda (install miniconda and put it on PATH)" >&2
  exit 1
fi
command -v claude >/dev/null || echo "WARNING: 'claude' not on PATH; the claude-backed agents will fail to run."
if grep -q '"backend": *"codex"' agents.json && ! command -v codex >/dev/null; then
  echo "WARNING: 'codex' not on PATH; the codex-backed agents in agents.json will fail to run."
fi

# 2. Conda env and dependencies.
if conda env list | awk '{print $1}' | grep -qx peon-chat; then
  echo "conda env 'peon-chat' already exists, skipping creation."
else
  conda create -n peon-chat python=3.12 -y
fi
conda run -n peon-chat pip install -r requirements.txt

# 3. The peon-chat command, a shim that execs the tracked script by absolute path.
mkdir -p "$HOME/.local/bin"
printf '#!/usr/bin/env bash\nexec "%s/bin/peon-chat" "$@"\n' "$REPO" > "$HOME/.local/bin/peon-chat"
chmod +x "$HOME/.local/bin/peon-chat"
echo "installed the 'peon-chat' command at $HOME/.local/bin/peon-chat (run 'peon-chat --help')"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "WARNING: $HOME/.local/bin is not on PATH; add it to your shell profile to use the 'peon-chat' command." ;;
esac

# 4. Config file, then report which Slack tokens are still missing.
if [ -f .env ]; then
  echo ".env already exists, left untouched."
else
  cp -n .env.example .env
  echo "created .env from .env.example"
fi
conda run -n peon-chat python - <<'PY'
import json, re
# ponytail: naive .env line scan, no quote or export handling. Switch to dotenv if .env grows syntax.
vals = {}
for line in open(".env"):
    m = re.match(r"\s*(SLACK_(?:BOT|APP)_TOKEN_\w+)\s*=\s*(.*)", line)
    if m:
        vals[m.group(1)] = m.group(2).strip()
missing = []
for a in json.load(open("agents.json")):
    for kind in ("BOT", "APP"):
        var = f"SLACK_{kind}_TOKEN_{a['name'].upper()}"
        v = vals.get(var, "")
        # A placeholder from .env.example counts as unset.
        if not v or v.startswith(("xoxb-" + a["name"], "xapp-" + a["name"])):
            missing.append(var)
print("Slack tokens still to fill in .env:", ", ".join(missing) if missing else "none, all set.")
PY

# 5. Slack app manifests.
conda run -n peon-chat python -m src manifest --write
echo "Create one Slack app per agent at https://api.slack.com/apps ('Create New App' -> 'From a manifest'), pasting the matching manifests/manifest-<name>.json."

# 6. Service unit (opt-in).
if [ "$SERVICE" = 0 ]; then
  echo "Next: fill the Slack tokens in .env, then re-run './install.sh --service' to install the always-on service."
  exit 0
fi

case "$(uname -s)" in
  Darwin)
    dest="$HOME/Library/LaunchAgents/com.unarylab.peon-chat.plist"
    guard_dest "$dest"
    REPO="$REPO" CONDA_BIN="$(conda info --base)/bin" DEST="$dest" \
      conda run -n peon-chat python - <<'PY'
import os
repo = os.environ["REPO"]
text = open("deploy/com.unarylab.peon-chat.plist").read()
text = text.replace("/Users/YOU/Projects/peon-chat", repo)
path = "%s:%s/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin" % (os.environ["CONDA_BIN"], os.environ["HOME"])
# The log path is where `peon-chat logs` tails; both streams go to one file.
text = text.replace(
    "  <key>RunAtLoad</key>",
    "  <key>EnvironmentVariables</key>\n"
    "  <dict><key>PATH</key><string>%s</string></dict>\n"
    "  <key>StandardOutPath</key><string>%s/peon-chat.log</string>\n"
    "  <key>StandardErrorPath</key><string>%s/peon-chat.log</string>\n"
    "  <key>RunAtLoad</key>" % (path, repo, repo),
)
open(os.environ["DEST"], "w").write(text)
PY
    launchctl unload "$dest" 2>/dev/null || true
    launchctl load -w "$dest"
    echo "launchd service loaded: com.unarylab.peon-chat"
    ;;
  Linux)
    dest="$HOME/.config/systemd/user/peon-chat.service"
    guard_dest "$dest"
    sed -e "s|%h/Projects/peon-chat|$REPO|" \
        -e "s|/usr/bin/env conda|$(conda info --base)/bin/conda|" deploy/peon-chat.service > "$dest"
    # Linger keeps the user service running when no session is open.
    loginctl enable-linger "$USER" 2>/dev/null ||
      echo "WARNING: could not enable linger; the service stops when your last session ends (run 'sudo loginctl enable-linger $USER')."
    systemctl --user daemon-reload
    systemctl --user enable --now peon-chat
    systemctl --user restart peon-chat
    echo "systemd --user service enabled and restarted: peon-chat"
    ;;
  *)
    echo "install.sh: no service template for $(uname -s); run 'conda run -n peon-chat python -m src' yourself." >&2
    exit 1
    ;;
esac
