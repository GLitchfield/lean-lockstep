#!/usr/bin/env bash
# Install and enable the nightly check for one project as the current user's
# lockstep-nightly-<project id> timer (needs a systemd user session; on WSL enable systemd).
# Usage: systemd/install.sh [PROJECT_ROOT]   (default: the git checkout containing the cwd)
set -e
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${1:-$(git rev-parse --show-toplevel)}" && pwd)"
read -r id logs < <(cd "$root" && PYTHONPATH="$here/.." python3 -c '
from pathlib import Path
from lockstep.config import load_config
c = load_config(Path.cwd())
print(c.project_id, c.build_log_dir)')
unit="lockstep-nightly-$id"
mkdir -p ~/.config/systemd/user "$logs"
sed -e "s|@ROOT@|$root|g" -e "s|@LOG_DIR@|$logs|g" -e "s|@PROJECT@|$id|g" -e "s|@LOCKSTEP@|$here/../bin/lockstep|g" \
  "$here/lockstep-nightly.service" > ~/.config/systemd/user/"$unit".service
sed -e "s|Run the lockstep nightly check|Run the lockstep nightly check for $id|" \
  "$here/lockstep-nightly.timer" > ~/.config/systemd/user/"$unit".timer
systemctl --user daemon-reload
systemctl --user enable --now "$unit".timer
systemctl --user list-timers "$unit".timer --no-pager
