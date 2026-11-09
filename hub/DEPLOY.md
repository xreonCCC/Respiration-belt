# Installing on the Pi

Assumes the `hub` branch is deployed at `/home/xreon/hub`.

```bash
# Code
cp hub_node_protocol.py hub_usb_export.py rtc_sync.py ~/hub/
sudo apt install -y python3-serial

# udev: start the service when a Node's card appears
sudo cp udev/99-respact-node.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules

# systemd: set --destination to the real upload target first,
# and add --no-delete for the first runs
sudo cp systemd/hub-usb-export@.service /etc/systemd/system/
sudo systemctl daemon-reload

# Tests (no hardware needed)
cp -r tests ~/hub/
cd ~/hub && python3 -m unittest discover -s tests -v
```

`rtc_sync.py` replaces the hub branch's CP2102 version and keeps its
options, exit codes and state files, so `hub-rtc-sync.timer` works
unchanged. Do not install `99-respact-rtc.rules` or
`hub-rtc-sync-udev.service`: they only match CP2102 boards.

## Monitoring

```bash
journalctl -fu 'hub-usb-export@*'          # one line per step
cat ~/hub/state/usb_export/last.json       # last transfer: steps, kept files and reasons, deletes
cat ~/hub/state/node_battery.json          # battery state for /health
lsusb | grep 303a                          # Node enumerated?
```

## Manual runs

```bash
sudo python3 ~/hub/hub_usb_export.py --device /dev/sda1 \
    --destination /home/xreon/hub/upload --no-delete
python3 ~/hub/rtc_sync.py --check-only     # Node clock offset
python3 ~/hub/rtc_sync.py                  # set, then read back
```

## Notes

- The service runs as root because of mount. Ingest and uplink run as
  `xreon` through `runuser`, so archive files keep their usual owner.
- The card is mounted read-only at `/run/respact/node`, with `uid/gid` set
  to `xreon` so ingest can read it.
- `hub_usb_export.py` also syncs the Node's RTC on every plug-in when the
  Pi is NTP-synced.
