#!/usr/bin/with-contenv bash
set -eu
# Run after LinuxServer applies PUID/PGID, before the GUI service starts.
exec /command/s6-setuidgid abc /usr/bin/python3 /usr/local/share/kicad/pcm.py \
    seed /opt/kicad-addons /config
