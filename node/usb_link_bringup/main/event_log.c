/*
 * Every row is written with fopen("a") -> fprintf -> fflush -> fsync ->
 * fclose.
 * fsync makes FatFs write the data and the new file size, so a row survives a
 * power cut (unbuckling the belt cuts power). fclose leaves no open file when
 * USB needs the card.
 */

#include "event_log.h"

#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "record_time.h"
#include "sd_storage.h"

static const char *TAG = "event_log";

typedef struct {
    int64_t unix_ms;
    char event[28];
    char session_file[40];
    uint32_t rows;
    char detail[96];
} event_row_t;

static event_row_t s_pending[EVENT_LOG_PENDING_MAX];
static int s_pending_count;
static uint32_t s_pending_dropped;

/* Created on first use. Several tasks log events (recorder, export_fsm). */
static SemaphoreHandle_t s_lock;
static StaticSemaphore_t s_lock_storage;

static void lock(void) {
    if (s_lock == NULL) {
        /* The first call is from app_main before other tasks exist, so this
         * lazy init is not racy. */
        s_lock = xSemaphoreCreateMutexStatic(&s_lock_storage);
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
}

static void unlock(void) {
    xSemaphoreGive(s_lock);
}

static int64_t now_unix_ms(void) {
    if (!record_time_system_clock_valid()) {
        return -1; /* same convention as the existing EVENTS.CSV */
    }
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

/* CSV-quote a field: wrap in quotes, double any quote inside. */
static void write_quoted(FILE *file, const char *text) {
    fputc('"', file);
    for (const char *p = text; *p != '\0'; ++p) {
        if (*p == '"') {
            fputc('"', file);
        }
        fputc(*p, file);
    }
    fputc('"', file);
}

/* Caller holds the lock and the app owns the card. */
static esp_err_t append_row(const event_row_t *row) {
    FILE *file = fopen(SD_STORAGE_MOUNT_POINT "/" EVENT_LOG_FILE, "a");
    if (file == NULL) {
        return ESP_FAIL;
    }
    fprintf(file, "%lld,", (long long)row->unix_ms);
    write_quoted(file, row->event);
    fputc(',', file);
    write_quoted(file, row->session_file);
    fprintf(file, ",%lu,", (unsigned long)row->rows);
    write_quoted(file, row->detail);
    fputc('\n', file);

    const bool ok = fflush(file) == 0 && fsync(fileno(file)) == 0;
    fclose(file);
    return ok ? ESP_OK : ESP_FAIL;
}

esp_err_t event_log_write(const char *event, const char *session_file, uint32_t rows,
                          const char *detail) {
    event_row_t row = {.unix_ms = now_unix_ms(), .rows = rows};
    snprintf(row.event, sizeof(row.event), "%s", event ? event : "");
    snprintf(row.session_file, sizeof(row.session_file), "%s", session_file ? session_file : "");
    snprintf(row.detail, sizeof(row.detail), "%s", detail ? detail : "");

    ESP_LOGI(TAG, "%s %s %s", row.event, row.session_file, row.detail);

    lock();
    esp_err_t err;
    if (sd_storage_owner() == SD_OWNER_APP) {
        err = append_row(&row);
    } else if (s_pending_count < EVENT_LOG_PENDING_MAX) {
        s_pending[s_pending_count++] = row;
        err = ESP_OK;
    } else {
        s_pending_dropped++;
        err = ESP_ERR_NO_MEM;
    }
    unlock();
    return err;
}

esp_err_t event_log_flush_pending(void) {
    lock();
    if (sd_storage_owner() != SD_OWNER_APP) {
        unlock();
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t result = ESP_OK;
    int written = 0;
    for (; written < s_pending_count; ++written) {
        if (append_row(&s_pending[written]) != ESP_OK) {
            result = ESP_FAIL;
            break;
        }
    }
    /* Keep whatever failed to write for the next attempt. */
    memmove(s_pending, s_pending + written, (size_t)(s_pending_count - written) * sizeof(event_row_t));
    s_pending_count -= written;

    if (result == ESP_OK && s_pending_dropped > 0) {
        event_row_t overflow = {.unix_ms = now_unix_ms(), .rows = 0};
        snprintf(overflow.event, sizeof(overflow.event), "EVENT_LOG_OVERFLOW");
        snprintf(overflow.detail, sizeof(overflow.detail), "dropped=%lu",
                 (unsigned long)s_pending_dropped);
        if (append_row(&overflow) == ESP_OK) {
            s_pending_dropped = 0;
        }
    }
    unlock();
    return result;
}
