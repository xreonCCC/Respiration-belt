# Merging into board_bringup_test

`usb_link_bringup` is a standalone app; the belt firmware is
`board_bringup_test`. These are the changes needed to merge the two.

## 1. Files

Copy into its `main/`: `node_protocol.c/.h`, `link_events.h`,
`usb_transport.c/.h`, `sd_storage.c/.h`, `export_fsm.c/.h`,
`event_log.c/.h`, `file_sha256.c/.h`, `record_time.c/.h`, `battery.c/.h`,
`board_pins.h`.

`app_main.c` is the fake recorder and is not copied; it is the example for
step 4.

## 2. Build files

- `main/CMakeLists.txt`: add the `.c` files above to `SRCS`, and
  `esp_driver_sdmmc sdmmc fatfs vfs esp_driver_i2c esp_driver_gpio
  esp_timer mbedtls nvs_flash` to `PRIV_REQUIRES`.
- `main/idf_component.yml`: add `espressif/esp_tinyusb: "==2.4.0"`.
- `sdkconfig.defaults`: append everything from
  `usb_link_bringup/sdkconfig.defaults` except `CONFIG_IDF_TARGET`, then
  run `idf.py fullclean`.

## 3. SD card ownership

`board_bringup_test` mounts the card itself (EVENTS.CSV shows
`TF_MOUNTED sdmmc`). Remove that mount and call `sd_storage_init()`
instead. esp_tinyusb's MSC storage must own the card handle to switch it
between the firmware and the USB host, and two mounts of the same card
would corrupt it. The mount point stays `/sdcard`, so existing paths do not
change.

## 4. Recorder hooks

Fill in `link_hooks_t` for the recorder, following `app_main.c`:

```c
static const link_hooks_t hooks = {
    .is_recording   = ...,  // a CSV is open
    .stop_recording = ...,  // fflush + fsync + fclose, log RECORDING_STOPPED
    .after_usb      = ...,  // start a new file, or stay idle
};
ESP_ERROR_CHECK(export_fsm_start(&hooks));   // after sd_storage_init()
```

The recorder must:

1. Check `export_fsm_recorder_may_use_card()` before opening any file,
   holding the same mutex that `stop_recording` takes for both the check
   and the open. Otherwise a button press as USB arrives can open a file on
   a card the Pi is about to read.
2. Never reuse a file number. Keep the counter in NVS (see
   `next_file_number()` in `app_main.c`), not only in a card scan.

It should also call `export_fsm_notify_recording(true/false)` when
recording starts or stops on its own (button, buckle), so `STATUS` stays
accurate.

## 5. I2C

`record_time_init(bus)` uses the new driver (`driver/i2c_master.h`) on
SDA1/SCL1 (IO15/IO16), shared with IMU1.

- IMU code on the new driver: pass its `i2c_master_bus_handle_t`.
- IMU code on the old driver (`driver/i2c.h`): the firmware aborts at boot
  with "CONFLICT! driver_ng", so one side has to be ported first.

## 6. Start order in `app_main`

1. `nvs_flash_init()`
2. `record_time_init()`, so the first EVENTS row has a real timestamp
3. `battery_init()`
4. `sd_storage_init()`, before USB, so the Pi never sees an empty drive
5. sensors and recorder
6. `export_fsm_start(&hooks)`, which starts USB last

## 7. Flashing

At boot TinyUSB takes the USB pins from the built-in USB-Serial-JTAG, so
the flashing port disappears while the app runs. Hold BOOT, tap RESET,
release BOOT, then run `idf.py flash`. Logs come out on UART0 only (TP3 =
TX, TP4 = RX, 115200) through a 3.3 V USB-UART adapter.
