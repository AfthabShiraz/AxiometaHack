#!/bin/bash
# One command for the whole teleop stack:
#   - quest_head.py  (Quest head-tracking -> servos + camera relay)
#     opens in its OWN Terminal window so its live readout stays visible
#   - drive_glove.py (glove -> motors console) runs HERE, interactive:
#     space=arm/e-stop  c=calibrate  k=keyboard mode  q=quit
# Quitting the drive console (q) also stops the quest hub.
#
# The rover, glove and ESP32-CAM are just devices — power them and they
# join the hotspot on their own. Nothing else to start.

ROOT="$(cd "$(dirname "$0")" && pwd)"

# replace any stale head-tracking hub so the port is free
pkill -f hub/quest_head.py 2>/dev/null && sleep 0.5

osascript >/dev/null <<EOF
tell application "Terminal"
  do script "cd '$ROOT' && exec python3 hub/quest_head.py"
end tell
EOF

echo "Quest hub opening in its own Terminal window."
echo "Headset: https://10.61.237.1:8443  (accept cert -> ENTER VR)"
echo
python3 "$ROOT/hub/drive_glove.py" "$@"

pkill -f hub/quest_head.py 2>/dev/null
echo "quest hub stopped."
