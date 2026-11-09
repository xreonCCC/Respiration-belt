#pragma once
/*
 * RespBelt PCB V1.0 pin map, from the 2026-08-13 schematic. If a pin here
 * disagrees with board_bringup_test, board_bringup_test is right.
 */

/* TF card, SDMMC 4-bit (10k pull-ups on the board: R25 R26 R31 R32 R34). */
#define PIN_SD_CMD 4
#define PIN_SD_CLK 5
#define PIN_SD_D0 6
#define PIN_SD_D1 7
#define PIN_SD_D2 8
#define PIN_SD_D3 9

/* I2C bus "SDA1/SCL1": IMU1 and the DS3231 RTC share it (4.7k pull-ups R27/R28). */
#define PIN_I2C1_SDA 15
#define PIN_I2C1_SCL 16
#define DS3231_I2C_ADDRESS 0x68

/* BQ25185 charger status, open-drain, 10k pull-ups R13/R14. */
#define PIN_CHG_STAT1 17
#define PIN_CHG_STAT2 18

/* Native USB: IO19 = D-, IO20 = D+. Fixed by the S3, no config needed. */

/* SW3 "STARTUP" push button to GND, 10k pull-up R35: pressed = LOW. */
#define PIN_STARTUP_BUTTON 2
/* LED2 "REC_INDICATOR" through 1k to GND: HIGH = LED on. */
#define PIN_REC_LED 40
