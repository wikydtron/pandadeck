/* BIGTREETECH Panda Touch — pin map + panel timing.
 * Source: fmauNeko/pandatouch-bsp (MIT) doc/HARDWARE.md + src/bsp_display.c,
 * cross-checked against a real unit (ESP32-S3R8, 16MB flash). */
#pragma once

#define PANEL_W 800
#define PANEL_H 480

/* 16-bit parallel RGB, DE mode (no HSYNC/VSYNC lines). The BSP's 23 MHz
 * shows jumping bands on this unit (bounce-buffer underruns through the
 * Arduino core's 32 KB / 32 B data cache). Tested on the desk unit with the
 * UI frozen: 16 MHz/10 lines better, 16 MHz/30 lines rarer, 0 lines drifts,
 * 14 MHz/30 lines clean (~33 Hz refresh, no visible flicker); 12 MHz and
 * below loses panel sync. Runtime-overridable via NVS (DisplayCfg). */
#define PANEL_PCLK_HZ   (14 * 1000 * 1000)
#define PANEL_PIN_PCLK  5
#define PANEL_PIN_DE    38
#define PANEL_PIN_RST   46
#define PANEL_PIN_BL    21

/* D0..D15 = B3..B7, G2..G7, R3..R7 (RGB565 bit order, LSB first). */
#define PANEL_DATA_PINS {17, 18, 48, 47, 39, 11, 12, 13, 14, 15, 16, 6, 7, 8, 9, 10}

#define PANEL_HSYNC_PW 4
#define PANEL_HSYNC_BP 8
#define PANEL_HSYNC_FP 8
#define PANEL_VSYNC_PW 4
#define PANEL_VSYNC_BP 16
#define PANEL_VSYNC_FP 16

/* Bounce buffer decouples PSRAM latency from the pixel clock (WiFi load
 * otherwise makes RGB panels drift/shift on the S3). 10 lines = 16 KB x2 SRAM. */
#define PANEL_BOUNCE_LINES 30

/* GT911 capacitive touch on I2C0. */
#define TOUCH_PIN_SDA 2
#define TOUCH_PIN_SCL 1
#define TOUCH_PIN_INT 40
#define TOUCH_PIN_RST 41

/* Expansion header I2C1 (unused for now). */
#define EXT_PIN_SDA 4
#define EXT_PIN_SCL 3

/* This unit's panel is mounted upside down relative to its scan direction
 * (confirmed on the desk unit: unrotated output reads upside down). */
#define DISPLAY_ROTATE_180 1
/* GT911 already reports in the user orientation (raw tap on DECK = 205,42). */
#define TOUCH_ROTATE_180 0

/* Backlight PWM (active-high; confirmed by a GPIO blink test). */
#define BL_PWM_FREQ 30000
#define BL_PWM_BITS 11
