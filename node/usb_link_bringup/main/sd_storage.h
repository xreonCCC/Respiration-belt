#pragma once
/*
 * TF card owner: the app (FAT mounted at /sdcard, host sees "medium not
 * present") or the USB host (FAT unmounted here, host reads the card over
 * MSC, read-only). One owner at a time, because FAT has no locking and each
 * side caches the file table.
 */

#include "esp_err.h"

#define SD_STORAGE_MOUNT_POINT "/sdcard" /* EVENTS.CSV paths already use /sdcard */
#define SD_STORAGE_MAX_OPEN_FILES 8

typedef enum {
    SD_OWNER_NONE = 0, /* no card, card unreadable, or a switch failed */
    SD_OWNER_APP,
    SD_OWNER_USB,
} sd_owner_t;

/* Init the SDMMC host + card, register it as the MSC LUN, mount FAT for the
 * app. Never formats. Owner afterwards: SD_OWNER_APP, or NONE on failure. */
esp_err_t sd_storage_init(void);

/* Precondition: every FILE* on the card is closed. Unmounts FAT here and
 * exposes the card to the USB host. */
esp_err_t sd_storage_give_to_usb(void);

/* Hides the card from the host ("medium not present") and mounts FAT here. */
esp_err_t sd_storage_take_for_app(void);

sd_owner_t sd_storage_owner(void);
const char *sd_storage_owner_name(sd_owner_t owner);
