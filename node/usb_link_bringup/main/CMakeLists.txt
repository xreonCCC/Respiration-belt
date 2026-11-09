idf_component_register(
    SRCS
        "app_main.c"
        "node_protocol.c"
        "usb_transport.c"
        "sd_storage.c"
        "export_fsm.c"
        "event_log.c"
        "file_sha256.c"
        "record_time.c"
        "battery.c"
    INCLUDE_DIRS "."
    # esp_tinyusb comes from idf_component.yml and is added automatically.
    PRIV_REQUIRES
        esp_driver_sdmmc   # sdmmc_host_init / slot config
        sdmmc              # sdmmc_card_init
        fatfs vfs          # FAT on /sdcard
        esp_driver_i2c     # DS3231 (new i2c_master driver)
        esp_driver_gpio    # button, LED, charger pins
        esp_timer
        nvs_flash          # persistent file counter
        mbedtls            # SHA-256
)
