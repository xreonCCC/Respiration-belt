#pragma once
/*
 * BATT? support from the BQ25185 STAT1/STAT2 pins.
 *
 * RespBelt V1.0 has no VBAT connection to an ADC, so PERCENT and VOLTAGE_MV
 * are reported as -1.
 */

#include "esp_err.h"

typedef enum {
    BATT_CHARGE_UNKNOWN = 0,
    BATT_CHARGE_IDLE,     /* complete, sleeping, or no USB power (STAT1=H STAT2=H) */
    BATT_CHARGE_CHARGING, /* STAT1=H STAT2=L */
    BATT_CHARGE_FAULT,    /* recoverable fault: input OVP, battery too hot/cold (L,H) */
    BATT_CHARGE_LATCHED,  /* latch-off fault: safety timer, short, overcurrent (L,L) */
} batt_charge_t;

typedef struct {
    int percent;    /* -1: not measurable on V1.0 */
    int voltage_mv; /* -1: not measurable on V1.0 */
    batt_charge_t charge;
} batt_status_t;

esp_err_t battery_init(void);
esp_err_t battery_read(batt_status_t *out);
const char *battery_charge_name(batt_charge_t charge);

/* CHARGING= field of the BATT reply: 1 while charging, else 0. */
int battery_charging_flag(const batt_status_t *status);
