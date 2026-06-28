#!/bin/bash
# Deploy the in-repo systemd unit files to their live locations.
# The repo is the source of truth: edit the *.service / *.timer files here,
# then run `sudo ./install.sh` to push the changes onto the Pi.
#
# Note: unlike the roast_dashboard/pi5 chain, the Python here is NOT copied
# anywhere — the units run it in place via WorkingDirectory/ExecStart pointing
# at this repo dir. So this installer only syncs unit files and (if any
# changed) reloads systemd. Updating the code is just `git pull` in place.
set -euo pipefail

# Re-exec under sudo if not already root.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# The unit files hardcode this path in WorkingDirectory/ExecStart/ReadWritePaths.
# If the repo lives somewhere else, the deployed units would point at the wrong
# place, so refuse rather than silently install broken units.
EXPECTED_DIR="/home/doeichel/sensor_node"
if [ "$REPO_DIR" != "$EXPECTED_DIR" ]; then
    echo "ERROR: repo is at $REPO_DIR but the unit files hardcode $EXPECTED_DIR." >&2
    echo "       Move the repo to $EXPECTED_DIR (or update the unit paths) first." >&2
    exit 1
fi

echo "== Deploying unit files from $REPO_DIR =="

# systemd unit files -> /etc/systemd/system (non-executable, mode 644).
changed_units=0
for f in "$REPO_DIR"/*.service "$REPO_DIR"/*.timer; do
    [ -e "$f" ] || continue
    dest="/etc/systemd/system/$(basename "$f")"
    if [ -f "$dest" ] && cmp -s "$f" "$dest"; then
        echo "unchanged: $dest"
    else
        install -D -m 644 "$f" "$dest"
        echo "installed: $dest (mode 644)"
        changed_units=1
    fi
done

if [ "$changed_units" -eq 1 ]; then
    echo "== Reloading systemd (unit files changed) =="
    systemctl daemon-reload
else
    echo "== No unit changes; skipping daemon-reload =="
fi

echo "Done."
echo "  - Units read MySQL/MQTT settings from /etc/roast.env (managed separately)."
echo "  - After a unit *content* change, restart the affected service, e.g.:"
echo "        systemctl restart roast-sensor-mqtt.service"
echo "  - First-time enable:"
echo "        systemctl enable --now roast-sensor.service roast-sensor-mqtt.service"
echo "        systemctl enable --now roast-gas-cleanup.timer"
