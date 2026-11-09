#pragma once
/*
 * DS3231 RTC for RTCSET/RTCGET and CSV timestamps, on the new I2C driver
 * (driver/i2c_master.h). ESP-IDF aborts at boot ("CONFLICT! driver_ng") if the
 * old driver/i2c.h is used as well. If the IMU code on the same bus uses the
 * new driver, pass its bus handle to record_time_init(); otherwise one of the
 * two has to be ported.
 */

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

typedef enum {
    RTC_STATE_UNSET = 0, /* never set, or the oscillator stopped: time is meaningless */
    RTC_STATE_SYNCED,    /* set since the last oscillator stop, and after 2024 */
    RTC_STATE_ERROR,     /* chip not answering on I2C */
} rtc_state_t;

/* bus: an existing bus on SDA1/SCL1 to share, or NULL to create one here.
 * If the RTC holds a valid time, also sets the ESP32 system clock from it. */
esp_err_t record_time_init(i2c_master_bus_handle_t bus);

/* RTCSET: write the chip, clear its Oscillator Stop Flag, set the system clock. */
esp_err_t record_time_set_epoch(int64_t epoch_s);

/* RTCGET: read the chip itself (proves the write reached it, not just RAM). */
esp_err_t record_time_get_epoch(int64_t *epoch_s, rtc_state_t *state);

/* True once the system clock was set from a SYNCED RTC or by RTCSET.
 * event_log uses this instead of reading I2C for every row. */
bool record_time_system_clock_valid(void);

const char *record_time_state_name(rtc_state_t state);
