#pragma once
#include <stdint.h>
#include <stddef.h>
#include <lvgl.h>

/* Brings up the RGB panel (2 PSRAM framebuffers + bounce buffers), LVGL in
 * direct/double-buffer mode synced to vsync, GT911 touch and the backlight.
 * Returns false if the panel could not be created. */
bool display_init();

/* 0..100. Smoothly ramps; safe to call often. */
void display_set_brightness(uint8_t pct);
uint8_t display_brightness();

/* Milliseconds since the last touch (for idle dimming). */
uint32_t display_idle_ms();
/* Mark activity without a touch (e.g. an incoming alert should wake it). */
void display_poke();

/* Remote testing: pretend a finger is down at (x,y) for `ms` milliseconds.
 * Thread-safe (called from the HTTP task). */
void display_inject_touch(int x, int y, int ms);

/* The framebuffer currently being scanned out (RGB565 LE, PANEL_W*PANEL_H).
 * Used by the /api/screen debug endpoint. */
const uint16_t *display_front_buffer();
/* A consistent copy of the last completed frame (PSRAM, owned by display). */
const uint16_t *display_snapshot(uint32_t timeout_ms);

struct DisplayStats {
  uint32_t frames;
  uint32_t fb_done;   /* RGB driver "frame buffer complete" ISR count */
  uint32_t touches;
  uint16_t touch_w, touch_h;
  int16_t raw_x, raw_y; /* last GT911 coordinates before rotation */
  uint8_t bl_now;
  uint8_t bl_mode;    /* 0 none, 1 LEDC PWM, 2 GPIO on/off fallback */
  bool bl_override;
  bool touch_ok;
};
DisplayStats display_stats();

/* Debug probing (HTTP task). gpio: drive one of a few unassigned pins
 * (0/3/4/21/42/45/46) high/low; returns false for any other pin.
 * bl: raw LEDC duty on the backlight pin (0..2048), or release=true to hand
 * the pin back to the brightness logic with the given polarity. */
bool display_debug_gpio(int pin, int level);
void display_debug_bl(int duty, bool invert, bool release);
/* Panel streaming config, persisted in NVS ("disp"), applied at boot. */
struct DisplayCfg {
  uint8_t bounce_lines; /* 0 = no bounce buffer (DMA reads PSRAM directly) */
  uint32_t pclk_hz;
  bool pclk_neg;        /* sample on the falling edge */
};
DisplayCfg display_cfg();

/* 180-degree orientation flip for picture + touch, persisted in NVS.
 * request_* is thread-safe; apply_* runs on the LVGL thread (ui timer). */
bool display_flipped();
bool display_touch_flipped();
void display_request_flip(bool flip);
/* The picture flip in effect once a pending request is drawn. */
bool display_flip_wanted();
/* Removes the persisted flip / touch flip (settings reset; reboot after). */
void display_forget_orientation();
void display_set_touch_flip(bool flip);
void display_debug_lcd_reset();
void display_apply_pending_flip();
bool display_save_cfg(const DisplayCfg &c);

/* Change the RGB pixel clock live (8-30 MHz). */
bool display_debug_pclk(uint32_t hz);
uint32_t display_pclk();

/* When true the main loop stops running LVGL (no redraws at all): used to
 * tell signal-timing flicker apart from render/PSRAM-contention flicker. */
extern volatile bool g_ui_frozen;
