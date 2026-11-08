#!/usr/bin/env bash
# omAIOpanel installer: Python deps + NVML binding + USB access + systemd user
# unit + desktop launcher. Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

echo "== omAIOpanel installer =="

# 1. Python dependencies (system packages — need sudo)
MISSING=$(pacman -T python-pillow python-numpy python-psutil python-pyusb || true)
if [ -n "$MISSING" ]; then
    echo "-- installing Python deps: $MISSING"
    sudo pacman -S --needed $MISSING
else
    echo "-- Python deps already present"
fi

# 2. NVML binding for NVIDIA GPUs (not packaged on Arch; fresh Omarchy has no pip)
if /usr/bin/python -c "import pynvml" 2>/dev/null; then
    echo "-- pynvml already present"
elif command -v uv >/dev/null 2>&1; then
    echo "-- installing pynvml via uv (user site)"
    USERSITE=$(/usr/bin/python -c "import sysconfig; print(sysconfig.get_path('purelib', scheme='posix_user'))")
    uv pip install --python /usr/bin/python --target "$USERSITE" pynvml
else
    echo "!! NVIDIA users: install the NVML binding manually, e.g."
    echo "   uv pip install --python /usr/bin/python --target ~/.local/lib/python3.x/site-packages pynvml"
fi

# 3. USB access for the AIO panel (GrandVision 360, 87ad:70db)
RULE=/etc/udev/rules.d/99-omAIOpanel.rules
if grep -ls 'idVendor=="87ad"' /etc/udev/rules.d/*.rules 2>/dev/null | grep -q .; then
    echo "-- udev rule already present"
else
    echo "-- adding udev rule + triggering (sudo)"
    echo "SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"87ad\", ATTRS{idProduct}==\"70db\", OWNER=\"$USER\", GROUP=\"$USER\", MODE=\"0660\"" \
        | sudo tee "$RULE" > /dev/null
    sudo udevadm control --reload
    sudo udevadm trigger
fi

# 4. systemd user unit (the render daemon)
cp omAIOpanel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now omAIOpanel.service
sleep 2
systemctl --user status omAIOpanel.service --no-pager | head -6 || true

# 5. Desktop launcher entry
mkdir -p ~/.local/share/applications
cp omAIOpanel-editor.desktop ~/.local/share/applications/

echo
echo "Done."
echo "  Daemon : systemctl --user {status,restart,stop} omAIOpanel"
echo "  CLI    : python -m aio preview | once --push | restore   (from this dir)"
echo "  Editor : 'omAIOpanel editor' in your app launcher, or: python editor.py"
echo "  Config : ~/.config/omAIOpanel/config.toml"
