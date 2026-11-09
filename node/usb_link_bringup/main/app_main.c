/*
 * Bring-up firmware for the USB link on RespBelt V1.0. A fake recorder writes
 * synthetic resp_v2 rows (verdict SYNTHETIC_DATA on the Hub), so copy ->
 * upload -> delete can run without the sensor code. The STARTUP button toggles
 * recording.
 *
 * The recorder is also the reference for link_hooks_t: start and stop run
 * under s_rec_lock, and start checks export_fsm_recorder_may_use_card() while
 * holding it.
 */

#include <dirent.h>
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#include "battery.h"
#include "board_pins.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_log.h"
#include "export_fsm.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "record_time.h"
#include "sd_storage.h"

static const char *TAG = "bringup";

#define BRINGUP_AUTOSTART_RECORDING 1 /* start recording at boot, like a worn belt */
#define ROW_INTERVAL_MS 100           /* 10 rows/s */
#define FSYNC_INTERVAL_US 1000000     /* at most 1 s of data lost on power cut */
#define BUTTON_DEBOUNCE_MS 50
#define BUTTON_GUARD_MS 1500          /* same anti-double-toggle as the old firmware */

/* Header copied from a real board_bringup_test file (R0000007.CSV). */
static const char CSV_HEADER[] =
    "mono_us,unix_ms,seq,schema_version,data_source,"
    "chest_belt_low_sample_us,chest_belt_low_valid,chest_belt_low_raw,chest_belt_low_uv,"
    "chest_belt_high_sample_us,chest_belt_high_valid,chest_belt_high_raw,chest_belt_high_uv,"
    "abdomen_belt_low_sample_us,abdomen_belt_low_valid,abdomen_belt_low_raw,abdomen_belt_low_uv,"
    "abdomen_belt_high_sample_us,abdomen_belt_high_valid,abdomen_belt_high_raw,abdomen_belt_high_uv,"
    "chest_sample_us,chest_valid,chest_ax_g,chest_ay_g,chest_az_g,chest_gx_dps,chest_gy_dps,"
    "chest_gz_dps,chest_roll_deg,chest_pitch_deg,chest_yaw_deg,"
    "abdomen_sample_us,abdomen_valid,abdomen_ax_g,abdomen_ay_g,abdomen_az_g,abdomen_gx_dps,"
    "abdomen_gy_dps,abdomen_gz_dps,abdomen_roll_deg,abdomen_pitch_deg,abdomen_yaw_deg,status\n";

static SemaphoreHandle_t s_rec_lock;
static FILE *s_rec_file;
static char s_rec_path[48];
static uint32_t s_rec_rows;
static uint32_t s_seq;
static int64_t s_last_fsync_us;

/* File numbers must never repeat. After the Hub's deletes the card is
 * empty, so a scan alone would restart at R0000001, and ingest would
 * overwrite an archived R0000001.CSV in the same date folder. The counter
 * lives in NVS; the scan covers an erased NVS. */
static unsigned highest_on_card(void) {
    unsigned highest = 0;
    DIR *dir = opendir(SD_STORAGE_MOUNT_POINT);
    if (dir == NULL) {
        return 0;
    }
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        unsigned n = 0;
        char tail[8] = "";
        if ((entry->d_name[0] == 'R' || entry->d_name[0] == 'r') &&
            sscanf(entry->d_name + 1, "%7u.%3s", &n, tail) == 2 && n > highest) {
            highest = n;
        }
    }
    closedir(dir);
    return highest;
}

static unsigned next_file_number(void) {
    uint32_t next = 1;
    nvs_handle_t nvs;
    const bool have_nvs = nvs_open("usb_link", NVS_READWRITE, &nvs) == ESP_OK;
    if (have_nvs) {
        nvs_get_u32(nvs, "next_r", &next); /* stays 1 if never stored */
    }
    const unsigned on_card = highest_on_card() + 1;
    if (on_card > next) {
        next = on_card;
    }
    if (have_nvs) {
        nvs_set_u32(nvs, "next_r", next + 1);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
    return next;
}

/* Caller holds s_rec_lock. */
static esp_err_t start_recording_locked(void) {
    if (s_rec_file != NULL) {
        return ESP_OK;
    }
    if (!export_fsm_recorder_may_use_card()) {
        ESP_LOGW(TAG, "card busy (USB session), not starting");
        return ESP_ERR_INVALID_STATE;
    }
    snprintf(s_rec_path, sizeof(s_rec_path), "%s/R%07u.CSV", SD_STORAGE_MOUNT_POINT,
             next_file_number());
    s_rec_file = fopen(s_rec_path, "w");
    if (s_rec_file == NULL) {
        ESP_LOGE(TAG, "cannot create %s", s_rec_path);
        return ESP_FAIL;
    }
    fputs(CSV_HEADER, s_rec_file);
    s_rec_rows = 0;
    s_last_fsync_us = esp_timer_get_time();
    gpio_set_level(PIN_REC_LED, 1);
    event_log_write("RECORDING_STARTED", s_rec_path, 0, "synthetic_test");
    export_fsm_notify_recording(true);
    return ESP_OK;
}

/* Caller holds s_rec_lock. */
static esp_err_t stop_recording_locked(void) {
    if (s_rec_file == NULL) {
        return ESP_OK;
    }
    const bool synced = fflush(s_rec_file) == 0 && fsync(fileno(s_rec_file)) == 0;
    const bool closed = fclose(s_rec_file) == 0;
    s_rec_file = NULL;
    gpio_set_level(PIN_REC_LED, 0);

    struct stat st = {0};
    stat(s_rec_path, &st);
    char detail[64];
    snprintf(detail, sizeof(detail), "verified=%d bytes=%ld source=synthetic_test",
             (synced && closed) ? 1 : 0, (long)st.st_size);
    event_log_write("RECORDING_STOPPED", s_rec_path, s_rec_rows, detail);
    export_fsm_notify_recording(false);
    return (synced && closed) ? ESP_OK : ESP_FAIL;
}

/* Caller holds s_rec_lock. Synthetic breathing: 0.25 Hz (15 breaths/min). */
static void write_row_locked(void) {
    const int64_t now_us = esp_timer_get_time();
    int64_t unix_ms = -1;
    if (record_time_system_clock_valid()) {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        unix_ms = (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
    }
    const double t = (double)now_us / 1e6;
    const long chest = (long)(-6300 + 400 * sin(2 * M_PI * 0.25 * t));
    const long abdomen = (long)(-5200 + 350 * sin(2 * M_PI * 0.25 * t + 0.6));

    /* belt raw -> uV uses the same ~0.244 ratio seen in the real files */
    fprintf(s_rec_file,
            "%lld,%lld,%" PRIu32 ",resp_v2,synthetic_test,"
            "%lld,1,%ld,%ld,%lld,1,%ld,%ld,"
            "%lld,1,%ld,%ld,%lld,1,%ld,%ld,"
            "0,0,nan,nan,nan,nan,nan,nan,nan,nan,nan,"
            "0,0,nan,nan,nan,nan,nan,nan,nan,nan,nan,0x00\n",
            (long long)now_us, (long long)unix_ms, s_seq++,
            (long long)now_us, chest, chest * 244 / 1000, (long long)now_us, chest * 35,
            chest * 35 * 244 / 1000,
            (long long)now_us, abdomen, abdomen * 244 / 1000, (long long)now_us, abdomen * 35,
            abdomen * 35 * 244 / 1000);
    s_rec_rows++;

    if (now_us - s_last_fsync_us >= FSYNC_INTERVAL_US) {
        fflush(s_rec_file);
        fsync(fileno(s_rec_file));
        s_last_fsync_us = now_us;
    }
}

static bool hook_is_recording(void) {
    xSemaphoreTake(s_rec_lock, portMAX_DELAY);
    const bool rec = s_rec_file != NULL;
    xSemaphoreGive(s_rec_lock);
    return rec;
}

static esp_err_t hook_stop_recording(void) {
    xSemaphoreTake(s_rec_lock, portMAX_DELAY);
    const esp_err_t err = stop_recording_locked();
    xSemaphoreGive(s_rec_lock);
    return err;
}

static esp_err_t hook_after_usb(bool was_recording) {
    /* Bring-up choice: resume if recording was on. The belt firmware may
     * stay idle instead. */
    if (!was_recording) {
        return ESP_OK;
    }
    xSemaphoreTake(s_rec_lock, portMAX_DELAY);
    const esp_err_t err = start_recording_locked();
    xSemaphoreGive(s_rec_lock);
    return err;
}

static const link_hooks_t s_hooks = {
    .is_recording = hook_is_recording,
    .stop_recording = hook_stop_recording,
    .after_usb = hook_after_usb,
};

static void recorder_task(void *arg) {
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    int64_t last_toggle_us = 0;
    int stable_level = 1, last_level = 1;
    int64_t level_since_us = 0;

    for (;;) {
        /* Button: debounce, act on the press edge, ignore presses inside the guard. */
        const int level = gpio_get_level(PIN_STARTUP_BUTTON);
        const int64_t now = esp_timer_get_time();
        if (level != last_level) {
            last_level = level;
            level_since_us = now;
        } else if (level != stable_level && now - level_since_us >= BUTTON_DEBOUNCE_MS * 1000) {
            stable_level = level;
            if (stable_level == 0 && now - last_toggle_us >= BUTTON_GUARD_MS * 1000) {
                last_toggle_us = now;
                xSemaphoreTake(s_rec_lock, portMAX_DELAY);
                if (s_rec_file != NULL) {
                    stop_recording_locked();
                } else {
                    start_recording_locked();
                }
                xSemaphoreGive(s_rec_lock);
            }
        }

        xSemaphoreTake(s_rec_lock, portMAX_DELAY);
        if (s_rec_file != NULL) {
            write_row_locked();
        }
        xSemaphoreGive(s_rec_lock);

        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(ROW_INTERVAL_MS));
    }
}

static void status_task(void *arg) {
    (void)arg;
    for (;;) {
        batt_status_t b = {0};
        battery_read(&b);
        char file[sizeof(s_rec_path)];
        xSemaphoreTake(s_rec_lock, portMAX_DELAY);
        snprintf(file, sizeof(file), "%s", s_rec_file ? s_rec_path : "-");
        const uint32_t rows = s_rec_rows;
        xSemaphoreGive(s_rec_lock);
        ESP_LOGI(TAG, "state=%s owner=%s file=%s rows=%" PRIu32 " charger=%s",
                 export_fsm_state_name(export_fsm_state()),
                 sd_storage_owner_name(sd_storage_owner()), file, rows,
                 battery_charge_name(b.charge));
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

void app_main(void) {
    s_rec_lock = xSemaphoreCreateMutex();

    /* NVS holds the file counter (see next_file_number). */
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase(); /* the card scan in next_file_number() covers this case */
        nvs_err = nvs_flash_init();
    }
    if (nvs_err != ESP_OK) {
        ESP_LOGW(TAG, "NVS unavailable (%s): file numbers fall back to the card scan",
                 esp_err_to_name(nvs_err));
    }

    const gpio_config_t led = {
        .pin_bit_mask = 1ULL << PIN_REC_LED,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&led);
    gpio_set_level(PIN_REC_LED, 0);
    const gpio_config_t button = {
        .pin_bit_mask = 1ULL << PIN_STARTUP_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE, /* board has R35 too */
    };
    gpio_config(&button);

    /* RTC first, so the first EVENTS row has a timestamp. Card and MSC
     * storage before USB, which export_fsm_start() brings up last. */
    record_time_init(NULL); /* not fatal: logs a warning if the RTC is missing */
    battery_init();
    const esp_err_t sd_err = sd_storage_init();
    event_log_write("BOOT", "", 0, "firmware=usb_link_bringup");
    if (sd_err == ESP_OK) {
        event_log_write("TF_MOUNTED", "", 0, "sdmmc");
    }

#if BRINGUP_AUTOSTART_RECORDING
    if (sd_err == ESP_OK) {
        xSemaphoreTake(s_rec_lock, portMAX_DELAY);
        start_recording_locked();
        xSemaphoreGive(s_rec_lock);
    }
#endif

    ESP_ERROR_CHECK(export_fsm_start(&s_hooks));
    xTaskCreate(recorder_task, "recorder", 4096, NULL, 4, NULL);
    xTaskCreate(status_task, "status", 3072, NULL, 1, NULL);
    ESP_LOGI(TAG, "bring-up running; plug USB into the Hub to export");
}
