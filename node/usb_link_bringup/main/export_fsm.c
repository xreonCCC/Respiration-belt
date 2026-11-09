/*
 * Export state machine task. 
 *   IDLE / RECORDING
 *     | host configures USB
 *   STOP_RECORDING -> FLUSH_AND_CLOSE_FILES -> UNMOUNT_NODE_SD   (one handler)
 *   USB_EXPORT              Pi mounts read-only, copies, verifies, uploads
 *     | first valid DELETE_OK (the Hub has finished reading)
 *   take card back -> DELETE_CONFIRMED_FILES -> WAIT_DELETE_CONFIRM
 *     | further DELETE_OK: re-hash, delete, ACK
 *     | USB detached
 *   EXIT_USB_EXPORT -> REMOUNT_NODE_SD -> RESUME_RECORDING -> RECORDING / IDLE
 *
 * s_state is written only by this task, except IDLE <-> RECORDING from
 * export_fsm_notify_recording(). Both take s_state_lock around the USB session
 * check, so the recorder cannot open a file while USB takes the card.
 */

#include "export_fsm.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

#include "battery.h"
#include "esp_log.h"
#include "event_log.h"
#include "file_sha256.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "link_events.h"
#include "node_protocol.h"
#include "record_time.h"
#include "sd_storage.h"
#include "usb_transport.h"

static const char *TAG = "export_fsm";

#define EXPORT_FSM_STACK_BYTES 6144
#define EXPORT_FSM_PRIORITY 5
#define PATH_MAX_LEN (sizeof(SD_STORAGE_MOUNT_POINT) + NODE_PROTO_MAX_FILENAME + 2)

static QueueHandle_t s_queue;
static const link_hooks_t *s_hooks;
static SemaphoreHandle_t s_state_lock;
static volatile link_state_t s_state = LINK_ST_IDLE;
static bool s_was_recording;

static bool in_usb_session(link_state_t st) {
    return st != LINK_ST_IDLE && st != LINK_ST_RECORDING;
}

static void set_state(link_state_t next) {
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    ESP_LOGI(TAG, "%s -> %s", export_fsm_state_name(s_state), export_fsm_state_name(next));
    s_state = next;
    xSemaphoreGive(s_state_lock);
}

static void reply(const char *line) {
    const esp_err_t err = usb_transport_send_line(line);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "reply not sent (%s): %s", esp_err_to_name(err), line);
    }
}

static void reply_delete_fail(const char *filename, const char *path, const char *reason) {
    char line[NODE_PROTO_MAX_LINE + 1];
    char detail[48];
    snprintf(detail, sizeof(detail), "reason=%s", reason);
    event_log_write("DELETE_FAILED", path, 0, detail);
    snprintf(line, sizeof(line), NODE_REPLY_DELETE_FAIL_FMT, filename, reason);
    reply(line);
}

static void on_usb_configured(void) {
    if (in_usb_session(s_state)) {
        return; /* re-enumeration during a session changes nothing */
    }
    if (sd_storage_owner() != SD_OWNER_APP) {
        /* No card (or a broken one). CDC still works: RTC sync, BATT?. */
        ESP_LOGW(TAG, "USB attached but no card to export");
        return;
    }

    /* Set under the lock: from here on export_fsm_recorder_may_use_card()
     * returns false, so the recorder cannot open a new file. */
    s_was_recording = s_hooks->is_recording();
    set_state(LINK_ST_STOP_RECORDING);
    esp_err_t err = s_hooks->stop_recording();

    set_state(LINK_ST_FLUSH_AND_CLOSE_FILES);
    /* stop_recording() closed the CSV and event_log closes EVENTS.CSV after
     * every row, so no file is open. */

    if (err == ESP_OK) {
        set_state(LINK_ST_UNMOUNT_NODE_SD);
        /* Logged while the app still owns the card, so the row is in the
         * EVENTS.CSV the Pi copies. */
        event_log_write("USB_EXPORT_STARTED", "", 0, s_was_recording ? "was_recording=1" : "");
        err = sd_storage_give_to_usb();
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "export failed: %s", esp_err_to_name(err));
        sd_storage_take_for_app(); /* best effort: get the card back */
        event_log_write("USB_EXPORT_FAILED", "", 0, esp_err_to_name(err));
        set_state(LINK_ST_ERROR);
        return;
    }
    set_state(LINK_ST_USB_EXPORT);
}

static void on_usb_detached(void) {
    if (!in_usb_session(s_state)) {
        return;
    }
    set_state(LINK_ST_EXIT_USB_EXPORT);

    set_state(LINK_ST_REMOUNT_NODE_SD);
    /* No-op if a DELETE_OK already took the card back. */
    const esp_err_t err = sd_storage_take_for_app();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "could not remount the card: %s", esp_err_to_name(err));
        set_state(LINK_ST_ERROR); /* recorder stays off until the card is checked */
        return;
    }
    event_log_flush_pending();
    event_log_write("USB_EXPORT_ENDED", "", 0, "");

    set_state(LINK_ST_RESUME_RECORDING);
    /* Leave the USB states before the hook runs; the recorder checks
     * export_fsm_recorder_may_use_card() when it opens a file. */
    set_state(LINK_ST_IDLE);
    s_hooks->after_usb(s_was_recording);
    if (s_hooks->is_recording()) {
        set_state(LINK_ST_RECORDING);
    }
}

static void handle_delete_ok(const node_cmd_t *cmd) {
    char path[PATH_MAX_LEN];
    char line[NODE_PROTO_MAX_LINE + 1];
    char detail[80];

    /* The parser rejects '/', '\\' and "..", so this is always a file in
     * the card's root. */
    snprintf(path, sizeof(path), "%s/%s", SD_STORAGE_MOUNT_POINT, cmd->filename);

    if (s_state != LINK_ST_USB_EXPORT && s_state != LINK_ST_WAIT_DELETE_CONFIRM) {
        snprintf(line, sizeof(line), NODE_REPLY_DELETE_FAIL_FMT, cmd->filename,
                 NODE_FAIL_WRONG_STATE);
        reply(line); /* no event row: the card may not be ours to write */
        return;
    }

    /* First DELETE_OK of a session: the Hub has finished reading, take the
     * card back. */
    if (sd_storage_owner() != SD_OWNER_APP) {
        if (sd_storage_take_for_app() != ESP_OK) {
            snprintf(line, sizeof(line), NODE_REPLY_DELETE_FAIL_FMT, cmd->filename,
                     NODE_FAIL_CARD_BUSY);
            reply(line);
            return;
        }
        event_log_flush_pending();
    }
    set_state(LINK_ST_DELETE_CONFIRMED_FILES);
    snprintf(detail, sizeof(detail), "sha256=%.64s", cmd->sha256_hex);
    event_log_write("DELETE_REQUESTED", path, 0, detail);

    /* EVENTS.CSV is never deleted: hub_ingest.py would replace the archived
     * copy with a fresh, shorter one. */
    if (strcmp(cmd->filename, "EVENTS.CSV") == 0) {
        reply_delete_fail(cmd->filename, path, NODE_FAIL_PROTECTED);
        set_state(LINK_ST_WAIT_DELETE_CONFIRM);
        return;
    }

    struct stat st;
    if (stat(path, &st) != 0) {
        /* Likely already deleted by an earlier session whose ACK got lost.
         * The Hub treats not_found as done. */
        reply_delete_fail(cmd->filename, path, NODE_FAIL_NOT_FOUND);
        set_state(LINK_ST_WAIT_DELETE_CONFIRM);
        return;
    }

    /* Re-hash: only the exact bytes the Hub archived may be deleted. A
     * newer file that reused the name fails this check. */
    char actual[FILE_SHA256_HEX_LEN + 1];
    const int64_t t0 = esp_log_timestamp();
    if (file_sha256_hex(path, actual) != ESP_OK) {
        reply_delete_fail(cmd->filename, path, NODE_FAIL_IO_ERROR);
        set_state(LINK_ST_WAIT_DELETE_CONFIRM);
        return;
    }
    ESP_LOGI(TAG, "hashed %s (%ld bytes) in %ld ms", cmd->filename, (long)st.st_size,
             (long)(esp_log_timestamp() - t0));
    if (strcmp(actual, cmd->sha256_hex) != 0) {
        reply_delete_fail(cmd->filename, path, NODE_FAIL_SHA_MISMATCH);
        set_state(LINK_ST_WAIT_DELETE_CONFIRM);
        return;
    }

    if (remove(path) != 0) {
        reply_delete_fail(cmd->filename, path, NODE_FAIL_IO_ERROR);
        set_state(LINK_ST_WAIT_DELETE_CONFIRM);
        return;
    }
    event_log_write("FILE_DELETED", path, 0, "");
    snprintf(line, sizeof(line), NODE_REPLY_DELETE_ACK_FMT, cmd->filename);
    reply(line);
    set_state(LINK_ST_WAIT_DELETE_CONFIRM);
}

static void handle_line(const char *text) {
    node_cmd_t cmd;
    char line[NODE_PROTO_MAX_LINE + 1];

    if (!node_proto_parse(text, &cmd)) {
        snprintf(line, sizeof(line), NODE_REPLY_ERR_FMT, cmd.error ? cmd.error : "invalid");
        reply(line);
        return;
    }

    switch (cmd.type) {
    case NODE_CMD_PING:
        reply(NODE_REPLY_PONG);
        break;

    case NODE_CMD_RTCSET: {
        int64_t old_epoch = -1;
        rtc_state_t old_state;
        record_time_get_epoch(&old_epoch, &old_state);
        if (record_time_set_epoch(cmd.epoch_s) != ESP_OK) {
            reply("ERR: rtc_write_failed");
            break;
        }
        char detail[64];
        snprintf(detail, sizeof(detail), "old=%lld new=%lld", (long long)old_epoch,
                 (long long)cmd.epoch_s);
        event_log_write("RTC_SET", "", 0, detail);
        snprintf(line, sizeof(line), NODE_REPLY_RTCSET_OK_FMT, cmd.epoch_s);
        reply(line);
        break;
    }

    case NODE_CMD_RTCGET: {
        int64_t epoch = -1;
        rtc_state_t st = RTC_STATE_ERROR;
        record_time_get_epoch(&epoch, &st);
        snprintf(line, sizeof(line), NODE_REPLY_RTC_STATUS_FMT, record_time_state_name(st), epoch);
        reply(line);
        break;
    }

    case NODE_CMD_BATT: {
        batt_status_t b = {.percent = -1, .voltage_mv = -1};
        battery_read(&b);
        snprintf(line, sizeof(line), NODE_REPLY_BATT_FMT, b.percent, b.voltage_mv,
                 battery_charging_flag(&b));
        reply(line);
        break;
    }

    case NODE_CMD_STATUS:
        snprintf(line, sizeof(line), NODE_REPLY_STATUS_FMT, export_fsm_state_name(s_state),
                 sd_storage_owner_name(sd_storage_owner()));
        reply(line);
        break;

    case NODE_CMD_DELETE_OK:
        handle_delete_ok(&cmd);
        break;

    case NODE_CMD_DELETE_ABORT:
        event_log_write("DELETE_ABORTED", cmd.filename, 0, "");
        snprintf(line, sizeof(line), NODE_REPLY_DELETE_ABORTED_FMT, cmd.filename);
        reply(line);
        break;

    default:
        reply("ERR: unknown_command");
        break;
    }
}

static void export_fsm_task(void *arg) {
    (void)arg;
    link_event_t event;
    for (;;) {
        if (xQueueReceive(s_queue, &event, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        switch (event.type) {
        case LINK_EVT_USB_CONFIGURED: on_usb_configured(); break;
        case LINK_EVT_USB_DETACHED: on_usb_detached(); break;
        case LINK_EVT_CDC_LINE: handle_line(event.line); break;
        }
    }
}

esp_err_t export_fsm_start(const link_hooks_t *hooks) {
    if (hooks == NULL || hooks->is_recording == NULL || hooks->stop_recording == NULL ||
        hooks->after_usb == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    s_hooks = hooks;
    s_state_lock = xSemaphoreCreateMutex();
    s_queue = xQueueCreate(LINK_EVENT_QUEUE_DEPTH, sizeof(link_event_t));
    if (s_state_lock == NULL || s_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    s_state = hooks->is_recording() ? LINK_ST_RECORDING : LINK_ST_IDLE;
    if (xTaskCreate(export_fsm_task, "export_fsm", EXPORT_FSM_STACK_BYTES, NULL,
                    EXPORT_FSM_PRIORITY, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return usb_transport_init(s_queue);
}

link_state_t export_fsm_state(void) {
    return s_state;
}

bool export_fsm_recorder_may_use_card(void) {
    if (s_state_lock == NULL) {
        return sd_storage_owner() == SD_OWNER_APP; /* before export_fsm_start() */
    }
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    const bool ok = !in_usb_session(s_state) && sd_storage_owner() == SD_OWNER_APP;
    xSemaphoreGive(s_state_lock);
    return ok;
}

void export_fsm_notify_recording(bool recording) {
    if (s_state_lock == NULL) {
        return;
    }
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    if (!in_usb_session(s_state)) {
        s_state = recording ? LINK_ST_RECORDING : LINK_ST_IDLE;
    }
    xSemaphoreGive(s_state_lock);
}

const char *export_fsm_state_name(link_state_t state) {
    switch (state) {
    case LINK_ST_IDLE: return "IDLE";
    case LINK_ST_RECORDING: return "RECORDING";
    case LINK_ST_STOP_RECORDING: return "STOP_RECORDING";
    case LINK_ST_FLUSH_AND_CLOSE_FILES: return "FLUSH_AND_CLOSE_FILES";
    case LINK_ST_UNMOUNT_NODE_SD: return "UNMOUNT_NODE_SD";
    case LINK_ST_USB_EXPORT: return "USB_EXPORT";
    case LINK_ST_DELETE_CONFIRMED_FILES: return "DELETE_CONFIRMED_FILES";
    case LINK_ST_WAIT_DELETE_CONFIRM: return "WAIT_DELETE_CONFIRM";
    case LINK_ST_EXIT_USB_EXPORT: return "EXIT_USB_EXPORT";
    case LINK_ST_REMOUNT_NODE_SD: return "REMOUNT_NODE_SD";
    case LINK_ST_RESUME_RECORDING: return "RESUME_RECORDING";
    case LINK_ST_ERROR: return "ERROR";
    }
    return "UNKNOWN";
}
