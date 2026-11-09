# Hardware test plan

Uses the `usb_link_bringup` firmware. Each stage depends on the one before
it, so stop at the first failure and send back the UART log and the Pi's
`journalctl` output.

Equipment: RespBelt V1.0 with a FAT32 TF card and a CR1632 cell, the Pi
with the `hub` branch, a 3.3 V USB-UART adapter on TP3 (TX), TP4 (RX) and
GND, and a USB-C cable.

## 0. Build and flash

```bash
cd node/usb_link_bringup
idf.py set-target esp32s3
idf.py build                  # first build downloads esp_tinyusb 2.4.0
# hold BOOT, tap RESET, release BOOT, then:
idf.py -p <port> flash
idf.py -p <uart adapter port> monitor
```

If the build log says `target esp32`, the shell has `IDF_TARGET=esp32` set
(the VS Code ESP-IDF extension does this), which overrides
`sdkconfig.defaults`. Set it to `esp32s3`, delete `build/` and build again.

Expected log: `TF card mounted at /sdcard`, `USB ready: MSC + CDC`,
`RECORDING_STARTED /sdcard/R000000N.CSV`, then a status line every 5 s.
The REC LED is on.

## 1. USB identity (Pi)

```bash
lsusb | grep 303a                         # 303a:4003 ... Sleep Monitor TF Card Export
ls -l /dev/serial/by-id/                  # ...Sleep_Monitor_TF_Card_Export_<MAC>-if00
lsblk                                     # a new sdX with partition sdX1
cat /sys/block/sdX/ro                     # 1 (write-protected)
```

## 2. Serial commands

```bash
python3 ~/hub/hub_node_protocol.py        # PING, STATUS, RTCGET, BATT?
python3 -m serial.tools.miniterm /dev/serial/by-id/*Sleep_Monitor* 115200
```

In miniterm: `PING` -> `PONG`, `RTCSET <date +%s>` -> `RTCSET OK`,
`RTCGET` -> `state=SYNCED`, `BATT?`, `STATUS`, `HELLO` ->
`ERR: unknown_command`.

Acceptance 12: after RTCSET, power-cycle the board; RTCGET still returns
the right time.

## 3. Export by hand, no deletes

```bash
sudo mount -o ro /dev/sdX1 /mnt && ls -l /mnt && sudo umount /mnt
```

As soon as the Pi enumerates the board, the UART log shows
`STOP_RECORDING -> ... -> USB_EXPORT` and the LED goes off.

Acceptance 2 and 3: the last CSV opens cleanly and ends with a complete
row; EVENTS.CSV has RECORDING_STOPPED followed by USB_EXPORT_STARTED.

## 4. Unplug and replug

Pull the cable while in USB_EXPORT. Within about 3 s the log shows
`EXIT_USB_EXPORT -> REMOUNT_NODE_SD -> ... RECORDING` and a new R file.

Acceptance 8: repeat in every reachable state; no file is lost and there
is no `card has no readable FAT` error.

## 5. Automatic export, dry run

Install per `hub/DEPLOY.md` with `--no-delete` added to the service. Plug
the Node in and run `journalctl -fu 'hub-usb-export@*'`.

Acceptance 1, 4 and 5: find_port, mount, scan, ingest, uplink, decide and
umount are all `[ok]`, the files are in `~/hub/archive/<date>/`, and
nothing is deleted.

## 6. Deletes

Remove `--no-delete`, run `systemctl daemon-reload` and replug.

Acceptance 9 and 10: the log shows `delete N/N deleted`, the files are gone
from the card, and EVENTS.CSV (still on the card) has DELETE_REQUESTED and
FILE_DELETED for each file.

## 7. Failure cases

- Acceptance 6: disconnect the Pi's network (or point `--destination` at a
  missing path) and plug the Node in. `uplink` fails, every R file is kept
  with `not_uploaded`, and the card still has them.
- Acceptance 7: reboot the Pi during a transfer. After boot the service
  runs again and finishes.
- Acceptance 11: modify one uploaded file on the Pi by hand and replug.
  That file is kept with `upload_mismatch`.
- Record the `hashed <file> (<bytes>) in <ms>` UART lines for a full-night
  file. The Hub's 300 s delete timeout must stay well above that time.

## Not covered by this firmware

- Acceptance 13 (battery on `/health`): the Hub writes
  `state/node_battery.json`; displaying it is part of the `/health` work.
- Acceptance 14 (RECORDING_WRITE_FAILED alarm): `hub_health.py` already
  checks EVENTS.CSV for it, but the fake recorder never writes it.
