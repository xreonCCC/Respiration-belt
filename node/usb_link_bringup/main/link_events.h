#pragma once
/*
 * Events from the USB callbacks to the export_fsm task.
 *
 * TinyUSB callbacks run in the TinyUSB task and must not block, so they only
 * queue one of these. export_fsm does the slow work and is the only code that
 * changes the SD card owner.
 */

#include "node_protocol.h"

typedef enum {
    LINK_EVT_USB_CONFIGURED, /* host sent SET_CONFIGURATION */
    LINK_EVT_USB_DETACHED,   /* host gone: unplug, or suspended for too long */
    LINK_EVT_CDC_LINE,       /* one complete command line in .line */
} link_event_type_t;

typedef struct {
    link_event_type_t type;
    char line[NODE_PROTO_MAX_LINE + 1]; /* LINK_EVT_CDC_LINE only, NUL-terminated */
} link_event_t;

#define LINK_EVENT_QUEUE_DEPTH 8
