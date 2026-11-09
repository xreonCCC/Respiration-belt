/*
 * TinyUSB setup and CDC line framing. esp_tinyusb builds the descriptors from
 * Kconfig and defines tud_mount_cb, tud_umount_cb, tud_suspend_cb and
 * tud_resume_cb itself (forwarding them to on_device_event()), so they must
 * not be defined here.
 */

#include "usb_transport.h"

#include <string.h>

#include "esp_log.h"
#include "freertos/timers.h"
#include "link_events.h"
#include "tinyusb.h"
#include "tinyusb_cdc_acm.h"
#include "tinyusb_default_config.h"

static const char *TAG = "usb_transport";

static QueueHandle_t s_queue;
static volatile bool s_connected;
static TimerHandle_t s_suspend_timer;

/* CDC line assembly. Only touched from the TinyUSB task (RX callback). */
static char s_rx_line[NODE_PROTO_MAX_LINE + 1];
static size_t s_rx_len;
static bool s_rx_overflow; /* true = discard bytes until the next '\n' */
static uint32_t s_dropped_events;

/* Called from the TinyUSB and timer tasks, so it never blocks. If
 * export_fsm falls behind, events are dropped and counted. */
static void post_event(const link_event_t *event) {
    if (s_queue == NULL || xQueueSend(s_queue, event, 0) != pdTRUE) {
        s_dropped_events++;
    }
}

static void post_simple(link_event_type_t type) {
    link_event_t event = {.type = type};
    post_event(&event);
}

/* USB packets do not line up with command lines, so bytes are collected
 * until '\n' and only complete lines are posted. */
static void on_rx_bytes(const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; ++i) {
        const char c = (char)data[i];
        if (c == '\r') {
            continue; /* accept "\r\n" from terminals like miniterm */
        }
        if (c == '\n') {
            if (!s_rx_overflow && s_rx_len > 0) {
                link_event_t event = {.type = LINK_EVT_CDC_LINE};
                memcpy(event.line, s_rx_line, s_rx_len);
                event.line[s_rx_len] = '\0';
                post_event(&event);
            }
            s_rx_len = 0;
            s_rx_overflow = false;
            continue;
        }
        if (s_rx_overflow) {
            continue;
        }
        if (s_rx_len < NODE_PROTO_MAX_LINE) {
            s_rx_line[s_rx_len++] = c;
        } else {
            /* Too long: drop the whole line. A truncated DELETE_OK could
             * still parse. */
            s_rx_overflow = true;
            s_rx_len = 0;
        }
    }
}

/* esp_tinyusb calls this from the TinyUSB task when new CDC data arrives. */
static void on_cdc_rx(int itf, cdcacm_event_t *event) {
    (void)event;
    static uint8_t buf[64]; /* static: keeps the USB task stack small */
    size_t got = 0;
    /* One callback per arrival, not per byte: drain everything waiting. */
    do {
        if (tinyusb_cdcacm_read((tinyusb_cdcacm_itf_t)itf, buf, sizeof(buf), &got) != ESP_OK) {
            return;
        }
        on_rx_bytes(buf, got);
    } while (got == sizeof(buf));
}

static void on_suspend_timeout(TimerHandle_t timer) {
    (void)timer;
    /* Still suspended after USB_DETACH_AFTER_SUSPEND_MS: cable is gone. */
    if (s_connected) {
        s_connected = false;
        ESP_LOGI(TAG, "suspended for %d ms: treating as unplug", USB_DETACH_AFTER_SUSPEND_MS);
        post_simple(LINK_EVT_USB_DETACHED);
    }
}

static void on_device_event(tinyusb_event_t *event, void *arg) {
    (void)arg;
    switch (event->id) {
    case TINYUSB_EVENT_ATTACHED: /* host sent SET_CONFIGURATION */
        xTimerStop(s_suspend_timer, 0);
        s_connected = true;
        post_simple(LINK_EVT_USB_CONFIGURED);
        break;
    case TINYUSB_EVENT_DETACHED: /* SET_CONFIGURATION(0) or a detected unplug */
        xTimerStop(s_suspend_timer, 0);
        if (s_connected) {
            s_connected = false;
            post_simple(LINK_EVT_USB_DETACHED);
        }
        break;
#ifdef CONFIG_TINYUSB_SUSPEND_CALLBACK
    case TINYUSB_EVENT_SUSPENDED:
        /* Unplug and bus sleep look the same without VBUS sense. A host
         * that is still there resumes the bus within the timeout. */
        xTimerReset(s_suspend_timer, 0);
        break;
#endif
#ifdef CONFIG_TINYUSB_RESUME_CALLBACK
    case TINYUSB_EVENT_RESUMED:
        xTimerStop(s_suspend_timer, 0);
        break;
#endif
    default:
        break;
    }
}

esp_err_t usb_transport_init(QueueHandle_t event_queue) {
    if (event_queue == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    s_queue = event_queue;

    s_suspend_timer = xTimerCreate("usb_susp", pdMS_TO_TICKS(USB_DETACH_AFTER_SUSPEND_MS),
                                   pdFALSE, NULL, on_suspend_timeout);
    if (s_suspend_timer == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* Full-speed, no VBUS monitoring (no sense pin on V1.0), descriptors
     * from Kconfig. */
    tinyusb_config_t config = TINYUSB_DEFAULT_CONFIG(on_device_event);
    esp_err_t err = tinyusb_driver_install(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_driver_install: %s", esp_err_to_name(err));
        return err;
    }

    const tinyusb_config_cdcacm_t cdc = {
        .cdc_port = TINYUSB_CDC_ACM_0,
        .callback_rx = on_cdc_rx,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL,
    };
    err = tinyusb_cdcacm_init(&cdc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_cdcacm_init: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "USB ready: MSC + CDC");
    return ESP_OK;
}

esp_err_t usb_transport_send_line(const char *line) {
    if (line == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_connected) {
        return ESP_ERR_INVALID_STATE;
    }
    const size_t len = strlen(line);
    if (tinyusb_cdcacm_write_queue(TINYUSB_CDC_ACM_0, (const uint8_t *)line, len) != len ||
        tinyusb_cdcacm_write_queue_char(TINYUSB_CDC_ACM_0, '\n') != 1) {
        return ESP_ERR_NO_MEM; /* TX buffer full: the Pi isn't reading */
    }
    /* Bounded, so a Pi that stops reading cannot block export_fsm. */
    return tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, pdMS_TO_TICKS(1000));
}

bool usb_transport_host_connected(void) {
    return s_connected;
}
