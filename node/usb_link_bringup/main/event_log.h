#pragma once
/*
 * Rows for /sdcard/EVENTS.CSV in board_bringup_test's format:
 *   unix_ms,event,session_file,rows,detail
 * unix_ms is -1 until the RTC is set. While USB owns the card, rows wait in
 * RAM for event_log_flush_pending().
 *
 * Events added by the USB link: USB_EXPORT_STARTED/ENDED/FAILED, RTC_SET,
 * DELETE_REQUESTED, FILE_DELETED, DELETE_FAILED, DELETE_ABORTED,
 * EVENT_LOG_OVERFLOW.
 */

#include <stdint.h>

#include "esp_err.h"

#define EVENT_LOG_FILE "EVENTS.CSV"
#define EVENT_LOG_PENDING_MAX 24

/* Safe from any task (internal mutex). */
esp_err_t event_log_write(const char *event, const char *session_file, uint32_t rows,
                          const char *detail);

/* Write rows held in RAM. Call right after sd_storage_take_for_app(). */
esp_err_t event_log_flush_pending(void);
