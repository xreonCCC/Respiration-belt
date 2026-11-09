/*
 * TF card ownership on top of esp_tinyusb 2.4.0, working around three of its
 * behaviours (see tinyusb_msc.c):
 * - It formats the card when mounting fails, unless do_not_format is set.
 * - By default it gives the card to USB on enumeration. auto_mount_off = 1
 *   turns that off; export_fsm switches after the recorder closes its files.
 * - tinyusb_msc_set_storage_mount_point() and tinyusb_msc_new_storage_sdmmc()
 *   can return ESP_OK on failure, so success is judged by the storage events
 *   and the handle. The event callback runs inside the switch call.
 */

#include "sd_storage.h"

#include "board_pins.h"
#include "driver/sdmmc_host.h"
#include "esp_log.h"
#include "sdmmc_cmd.h"
#include "tinyusb_msc.h"
#include "tusb.h" /* for the tud_msc_is_writable_cb prototype */

static const char *TAG = "sd_storage";

static sdmmc_card_t s_card;
static tinyusb_msc_storage_handle_t s_storage;
static sd_owner_t s_owner = SD_OWNER_NONE;

/* The library keeps this pointer without copying the string. */
static char s_base_path[] = SD_STORAGE_MOUNT_POINT;

/* Written by on_msc_event, read right after each switch call (same task). */
static volatile int s_last_event = -1;
static volatile bool s_format_required;

static void on_msc_event(tinyusb_msc_storage_handle_t handle, tinyusb_msc_event_t *event,
                         void *arg) {
    (void)handle;
    (void)arg;
    s_last_event = (int)event->id;
    if (event->id == TINYUSB_MSC_EVENT_FORMAT_REQUIRED) {
        s_format_required = true;
    }
}

/* TinyUSB asks this before writes and for MODE SENSE: never writable. */
bool tud_msc_is_writable_cb(uint8_t lun) {
    (void)lun;
    return false;
}

static esp_err_t switch_to(tinyusb_msc_mount_point_t target) {
    s_last_event = -1;
    s_format_required = false;
    tinyusb_msc_set_storage_mount_point(s_storage, target);

    if (s_format_required) {
        ESP_LOGE(TAG, "card has no readable FAT filesystem (not formatting it)");
        return ESP_ERR_NOT_FOUND;
    }
    if (s_last_event != TINYUSB_MSC_EVENT_MOUNT_COMPLETE) {
        ESP_LOGE(TAG, "switch to %s failed (event %d)",
                 target == TINYUSB_MSC_STORAGE_MOUNT_APP ? "APP" : "USB", s_last_event);
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t sd_storage_init(void) {
    /* Install the MSC driver first with these flags. Otherwise the storage
     * call below installs it with the defaults (auto-switch on). */
    const tinyusb_msc_driver_config_t driver = {
        .user_flags.auto_mount_off = 1,
        .callback = on_msc_event,
        .callback_arg = NULL,
    };
    esp_err_t err = tinyusb_msc_install_driver(&driver);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_msc_install_driver: %s", esp_err_to_name(err));
        return err;
    }

    /* SDMMC 4-bit on the PCB's pins (the S3 routes SDMMC through the GPIO
     * matrix). */
    sdmmc_host_t host = SDMMC_HOST_DEFAULT(); /* 20 MHz for now, you can raise later if stable */
    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 4;
    slot.clk = PIN_SD_CLK;
    slot.cmd = PIN_SD_CMD;
    slot.d0 = PIN_SD_D0;
    slot.d1 = PIN_SD_D1;
    slot.d2 = PIN_SD_D2;
    slot.d3 = PIN_SD_D3;
    slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP; /* board also has 10k pull-ups */

    err = sdmmc_host_init();
    if (err == ESP_OK) {
        err = sdmmc_host_init_slot(host.slot, &slot);
    }
    if (err == ESP_OK) {
        err = sdmmc_card_init(&host, &s_card);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "TF card init failed: %s (card missing?)", esp_err_to_name(err));
        return err;
    }
    sdmmc_card_print_info(stdout, &s_card);

    /* Register the card as the MSC LUN and mount it for the app. */
    const tinyusb_msc_storage_config_t config = {
        .medium.card = &s_card,
        .mount_point = TINYUSB_MSC_STORAGE_MOUNT_APP,
        .fat_fs = {
            .base_path = s_base_path,
            .config = {
                .format_if_mount_failed = false,
                .max_files = SD_STORAGE_MAX_OPEN_FILES,
                .allocation_unit_size = 0,
            },
            .do_not_format = true, /* never format a card with recordings */
            .format_flags = 0,
        },
    };
    s_storage = NULL;
    s_last_event = -1;
    s_format_required = false;
    tinyusb_msc_new_storage_sdmmc(&config, &s_storage);
    if (s_storage == NULL) { /* return code is unreliable, check the handle */
        ESP_LOGE(TAG, "MSC storage could not be created");
        return ESP_FAIL;
    }
    if (s_format_required || s_last_event != TINYUSB_MSC_EVENT_MOUNT_COMPLETE) {
        ESP_LOGE(TAG, "TF card has no usable FAT filesystem; format it as FAT32 on a PC");
        return ESP_ERR_NOT_FOUND;
    }

    s_owner = SD_OWNER_APP;
    ESP_LOGI(TAG, "TF card mounted at %s", SD_STORAGE_MOUNT_POINT);
    return ESP_OK;
}

esp_err_t sd_storage_give_to_usb(void) {
    if (s_owner == SD_OWNER_USB) {
        return ESP_OK; /* idempotent: export_fsm may call it twice */
    }
    if (s_owner != SD_OWNER_APP) {
        return ESP_ERR_INVALID_STATE;
    }
    const esp_err_t err = switch_to(TINYUSB_MSC_STORAGE_MOUNT_USB);
    s_owner = (err == ESP_OK) ? SD_OWNER_USB : SD_OWNER_NONE;
    return err;
}

esp_err_t sd_storage_take_for_app(void) {
    if (s_owner == SD_OWNER_APP) {
        return ESP_OK;
    }
    if (s_storage == NULL) {
        return ESP_ERR_INVALID_STATE; /* no card at boot */
    }
    /* From USB, or recovering from a failed switch (owner NONE): try to mount. */
    const esp_err_t err = switch_to(TINYUSB_MSC_STORAGE_MOUNT_APP);
    s_owner = (err == ESP_OK) ? SD_OWNER_APP : SD_OWNER_NONE;
    return err;
}

sd_owner_t sd_storage_owner(void) {
    return s_owner;
}

const char *sd_storage_owner_name(sd_owner_t owner) {
    switch (owner) {
    case SD_OWNER_APP: return "APP";
    case SD_OWNER_USB: return "USB";
    default: return "NONE";
    }
}
