#pragma once
/*
 * USB export state machine, run by one FreeRTOS task. State names follow the spec.
 * The steps from STOP_RECORDING to USB_EXPORT run in one handler, 
 * and the states exist so STATUS and the logs show where a failure happened.
 */

#include <stdbool.h>

#include "esp_err.h"

typedef enum {
    LINK_ST_IDLE = 0,              /* powered, not recording, no USB */
    LINK_ST_RECORDING,
    LINK_ST_STOP_RECORDING,
    LINK_ST_FLUSH_AND_CLOSE_FILES,
    LINK_ST_UNMOUNT_NODE_SD,
    LINK_ST_USB_EXPORT,            /* host owns the card and may read it */
    LINK_ST_DELETE_CONFIRMED_FILES,
    LINK_ST_WAIT_DELETE_CONFIRM,   /* Node owns the card again, accepting more DELETE_OK */
    LINK_ST_EXIT_USB_EXPORT,
    LINK_ST_REMOUNT_NODE_SD,
    LINK_ST_RESUME_RECORDING,
    LINK_ST_ERROR,                 /* card could not be handed over; recorder stays off */
} link_state_t;

/* Recorder callbacks. export_fsm never touches the recorder's files
 * directly. See app_main.c for an example. */
typedef struct {
    /* True while a measurement CSV is open. */
    bool (*is_recording)(void);

    /* Stop sampling; fflush + fsync + fclose the CSV; write RECORDING_STOPPED
     * to EVENTS.CSV. Return only once the file is closed.
     * Must be a no-op (ESP_OK) when not recording. */
    esp_err_t (*stop_recording)(void);

    /* USB session over, card back with the app. was_recording: recording
     * was on when USB arrived. Whether to start a new file is decided here. */
    esp_err_t (*after_usb)(bool was_recording);
} link_hooks_t;

/* Creates the queue + task, then starts USB. hooks must stay valid forever. */
esp_err_t export_fsm_start(const link_hooks_t *hooks);

link_state_t export_fsm_state(void);
const char *export_fsm_state_name(link_state_t state);

/* The recorder must check this before opening any file on the card. False
 * while a USB session owns, or is about to own, the card. */
bool export_fsm_recorder_may_use_card(void);

/* The recorder calls this when it starts/stops on its own (button, buckle),
 * so STATUS shows RECORDING vs IDLE. Ignored during a USB session. */
void export_fsm_notify_recording(bool recording);