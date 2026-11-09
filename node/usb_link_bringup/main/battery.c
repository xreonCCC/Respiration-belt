/*
 * BQ25185 STAT1/STAT2 decoding (datasheet SLUSF65, table 6-2). Both pins are
 * open-drain with pull-ups R13/R14, so L means the charger pulls it down.
 *
 *   STAT1 STAT2
 *    H     H    charge complete, sleep (no input), or charge disabled
 *    H     L    charging (incl. automatic recharge)
 *    L     H    recoverable fault (VIN over-voltage, TS hot/cold, thermal)
 *    L     L    latch-off fault (ISET/ILIM short, overcurrent, safety timer)
 *
 * STAT2 toggles when no battery is connected; a single sample does not detect
 * that. The pins also drive the charge LED, so they are inputs only.
 */

#include "battery.h"

#include "board_pins.h"
#include "driver/gpio.h"

esp_err_t battery_init(void) {
    const gpio_config_t config = {
        .pin_bit_mask = (1ULL << PIN_CHG_STAT1) | (1ULL << PIN_CHG_STAT2),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE, /* external 10k pull-ups */
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    return gpio_config(&config);
}

esp_err_t battery_read(batt_status_t *out) {
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    out->percent = -1;
    out->voltage_mv = -1;

    const int stat1 = gpio_get_level(PIN_CHG_STAT1);
    const int stat2 = gpio_get_level(PIN_CHG_STAT2);
    if (stat1 && stat2) {
        out->charge = BATT_CHARGE_IDLE;
    } else if (stat1 && !stat2) {
        out->charge = BATT_CHARGE_CHARGING;
    } else if (!stat1 && stat2) {
        out->charge = BATT_CHARGE_FAULT;
    } else {
        out->charge = BATT_CHARGE_LATCHED;
    }
    return ESP_OK;
}

const char *battery_charge_name(batt_charge_t charge) {
    switch (charge) {
    case BATT_CHARGE_IDLE: return "IDLE";
    case BATT_CHARGE_CHARGING: return "CHARGING";
    case BATT_CHARGE_FAULT: return "FAULT";
    case BATT_CHARGE_LATCHED: return "LATCHED_FAULT";
    default: return "UNKNOWN";
    }
}

int battery_charging_flag(const batt_status_t *status) {
    return (status != NULL && status->charge == BATT_CHARGE_CHARGING) ? 1 : 0;
}
