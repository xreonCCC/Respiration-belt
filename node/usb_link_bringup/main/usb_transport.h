#pragma once
/*
 * TinyUSB composite device (MSC + CDC-ACM, 303a:4003) on the native USB port,
 * using esp_tinyusb 2.4.0. Moves bytes and posts link_event_t; it does not
 * parse commands or touch the card.
 */

#include <stdbool.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

/* A bus suspend longer than this is treated as an unplug. RespBelt V1.0
 * cannot see VBUS, so an unplug only shows up as a suspend. */
#define USB_DETACH_AFTER_SUSPEND_MS 3000

/* Install TinyUSB and start posting link_event_t to event_queue. Call after
 * sd_storage_init(): the MSC storage must exist before the host enumerates
 * the device. */
esp_err_t usb_transport_init(QueueHandle_t event_queue);

/* Send one reply line: appends "\n" and flushes (1 s timeout). */
esp_err_t usb_transport_send_line(const char *line);

/* True between LINK_EVT_USB_CONFIGURED and LINK_EVT_USB_DETACHED. */
bool usb_transport_host_connected(void);
