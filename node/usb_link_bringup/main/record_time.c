/*
 * DS3231 registers used:
 *   0x00..0x06  sec, min, hour, weekday, day, month (+century bit), year; BCD
 *   0x0F        status; bit 7 = OSF (Oscillator Stop Flag)
 * OSF is set whenever the oscillator stopped, e.g. the CR1632 backup cell was
 * dead while main power was off. The time is then invalid.
 */

#include "record_time.h"

#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "board_pins.h"
#include "esp_log.h"

static const char *TAG = "record_time";

#define I2C_TIMEOUT_MS 100
#define REG_SECONDS 0x00
#define REG_STATUS 0x0F
#define STATUS_OSF 0x80
#define MIN_VALID_EPOCH 1704067200LL /* 2024-01-01, same floor as the Hub */

static i2c_master_dev_handle_t s_dev;
static volatile bool s_system_clock_valid;

static int bcd_to_int(uint8_t v) { return ((v >> 4) * 10) + (v & 0x0F); }
static uint8_t int_to_bcd(int v) { return (uint8_t)(((v / 10) << 4) | (v % 10)); }

/* Days since 1970-01-01 (proleptic Gregorian, UTC). From the old ds3231.c;
 * avoids timegm(), which newlib doesn't provide. */
static int64_t days_from_civil(int year, unsigned month, unsigned day) {
    year -= month <= 2;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = (unsigned)(year - era * 400);
    const unsigned doy = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return (int64_t)era * 146097 + (int)doe - 719468;
}

static esp_err_t decode_time(const uint8_t r[7], int64_t *epoch_s) {
    const int second = bcd_to_int(r[0] & 0x7F);
    const int minute = bcd_to_int(r[1] & 0x7F);
    int hour;
    if (r[2] & 0x40) { /* 12-hour mode */
        hour = bcd_to_int(r[2] & 0x1F) % 12 + ((r[2] & 0x20) ? 12 : 0);
    } else {
        hour = bcd_to_int(r[2] & 0x3F);
    }
    const int day = bcd_to_int(r[4] & 0x3F);
    const int month = bcd_to_int(r[5] & 0x1F);
    const int year = 2000 + bcd_to_int(r[6]) + ((r[5] & 0x80) ? 100 : 0);
    if (second > 59 || minute > 59 || hour > 23 || day < 1 || day > 31 || month < 1 ||
        month > 12) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    *epoch_s = days_from_civil(year, (unsigned)month, (unsigned)day) * 86400 + hour * 3600 +
               minute * 60 + second;
    return ESP_OK;
}

static esp_err_t read_regs(uint8_t first, uint8_t *out, size_t n) {
    return i2c_master_transmit_receive(s_dev, &first, 1, out, n, I2C_TIMEOUT_MS);
}

static esp_err_t set_system_clock(int64_t epoch_s) {
    const struct timeval tv = {.tv_sec = (time_t)epoch_s, .tv_usec = 0};
    if (settimeofday(&tv, NULL) != 0) {
        return ESP_FAIL;
    }
    s_system_clock_valid = true;
    return ESP_OK;
}

esp_err_t record_time_init(i2c_master_bus_handle_t bus) {
    if (bus == NULL) {
        const i2c_master_bus_config_t bus_config = {
            .i2c_port = -1, /* let the driver pick a free port */
            .sda_io_num = PIN_I2C1_SDA,
            .scl_io_num = PIN_I2C1_SCL,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .flags.enable_internal_pullup = false, /* board has 4.7k */
        };
        esp_err_t err = i2c_new_master_bus(&bus_config, &bus);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "I2C bus: %s", esp_err_to_name(err));
            return err;
        }
    }
    const i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = DS3231_I2C_ADDRESS,
        .scl_speed_hz = 100000,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &dev_config, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "add DS3231: %s", esp_err_to_name(err));
        return err;
    }

    int64_t epoch = 0;
    rtc_state_t state = RTC_STATE_ERROR;
    err = record_time_get_epoch(&epoch, &state);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "DS3231 not answering: %s", esp_err_to_name(err));
        return err;
    }
    if (state == RTC_STATE_SYNCED) {
        set_system_clock(epoch);
        ESP_LOGI(TAG, "system clock set from DS3231: epoch=%lld", (long long)epoch);
    } else {
        ESP_LOGW(TAG, "DS3231 time not valid yet; waiting for RTCSET from the Hub");
    }
    return ESP_OK;
}

esp_err_t record_time_set_epoch(int64_t epoch_s) {
    if (s_dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    const time_t t = (time_t)epoch_s;
    struct tm cal;
    /* The DS3231 stores years 2000-2199 via its century bit; we use 2000-2099. */
    if (epoch_s < MIN_VALID_EPOCH || gmtime_r(&t, &cal) == NULL || cal.tm_year < 100 ||
        cal.tm_year > 199) {
        return ESP_ERR_INVALID_ARG;
    }
    const uint8_t time_regs[] = {
        REG_SECONDS,
        int_to_bcd(cal.tm_sec),
        int_to_bcd(cal.tm_min),
        int_to_bcd(cal.tm_hour),         /* bit 6 = 0: 24-hour mode */
        (uint8_t)(cal.tm_wday + 1),      /* DS3231 weekday is 1..7 */
        int_to_bcd(cal.tm_mday),
        int_to_bcd(cal.tm_mon + 1),
        int_to_bcd(cal.tm_year - 100),
    };
    esp_err_t err = i2c_master_transmit(s_dev, time_regs, sizeof(time_regs), I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }

    /* Clear only OSF (read-modify-write), leaving the other status bits alone. */
    uint8_t status = 0;
    err = read_regs(REG_STATUS, &status, 1);
    if (err != ESP_OK) {
        return err;
    }
    const uint8_t clear_osf[] = {REG_STATUS, (uint8_t)(status & ~STATUS_OSF)};
    err = i2c_master_transmit(s_dev, clear_osf, sizeof(clear_osf), I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    return set_system_clock(epoch_s);
}

esp_err_t record_time_get_epoch(int64_t *epoch_s, rtc_state_t *state) {
    if (epoch_s == NULL || state == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *epoch_s = -1;
    *state = RTC_STATE_ERROR;
    if (s_dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    uint8_t regs[16]; /* 0x00..0x0F in one transfer: time + control + status */
    esp_err_t err = read_regs(REG_SECONDS, regs, sizeof(regs));
    if (err != ESP_OK) {
        return err;
    }
    int64_t epoch = -1;
    if (decode_time(regs, &epoch) != ESP_OK) {
        *state = RTC_STATE_UNSET; /* chip answers but the time is invalid */
        return ESP_OK;
    }
    *epoch_s = epoch;
    const bool osf = (regs[REG_STATUS] & STATUS_OSF) != 0;
    *state = (!osf && epoch >= MIN_VALID_EPOCH) ? RTC_STATE_SYNCED : RTC_STATE_UNSET;
    return ESP_OK;
}

bool record_time_system_clock_valid(void) {
    return s_system_clock_valid;
}

const char *record_time_state_name(rtc_state_t state) {
    switch (state) {
    case RTC_STATE_SYNCED: return "SYNCED";
    case RTC_STATE_UNSET: return "UNSET";
    default: return "ERROR";
    }
}
