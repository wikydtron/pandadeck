/* PandaDeck visual system: warm instrument-panel dark, Claude clay accent,
 * semantic status colors. Type: UI headings, serif numerals, mono (commands +
 * Claude spinner glyphs), Montserrat (small UI) and icon fonts: see font_*.c. */
#pragma once
#include <lvgl.h>

LV_FONT_DECLARE(font_head_28);
LV_FONT_DECLARE(font_head_48);
LV_FONT_DECLARE(font_serif_56);
LV_FONT_DECLARE(font_mono_16);
LV_FONT_DECLARE(font_mono_20);
LV_FONT_DECLARE(font_mono_32);
LV_FONT_DECLARE(font_icons_20);
LV_FONT_DECLARE(font_icons_36);
LV_FONT_DECLARE(font_icons_52); /* subset: solid transport + a few large icons */

#define COL_BG        0x141413
#define COL_SURFACE   0x1E1D1B
#define COL_SURFACE2  0x2A2926
#define COL_LINE      0x37352F
#define COL_TEXT      0xF0EEE6
#define COL_TEXT_DIM  0xA8A69E
#define COL_TEXT_FAINT 0x6E6C66
#define COL_CLAY      0xD97757 /* Claude / working */
#define COL_AMBER     0xF2B14B /* needs you */
#define COL_GREEN     0x5FB477 /* ok / approve */
#define COL_RED       0xE05A4F /* deny / error */
#define COL_BLUE      0x6FA8DC

static inline lv_color_t C(uint32_t hex) { return lv_color_hex(hex); }

#define TOPBAR_H 48
#define CONTENT_H (480 - TOPBAR_H)

/* Icon glyph (UTF-8) by name, e.g. icon("mic"). Unknown -> "". */
const char *icon(const char *name);

/* A bare container: no theme styles, no scrolling, no padding. */
lv_obj_t *ui_box(lv_obj_t *parent);
lv_obj_t *ui_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *txt);
/* Single line with "..." when too long. Needs the width set first; pins the
 * height to one line so LVGL's dots mode actually triggers instead of wrapping. */
void ui_one_line(lv_obj_t *label);
/* Change-aware setters: LVGL invalidates (= redraw + PSRAM traffic on this
 * panel) on every set, even with an identical value. These skip no-ops. */
void ui_set_text(lv_obj_t *label, const char *txt);
void ui_set_text_color(lv_obj_t *obj, uint32_t color);

/* Rounded surface card. */
lv_obj_t *ui_card(lv_obj_t *parent, uint32_t bg, int radius);
/* Slim progress bar with colored indicator. */
lv_obj_t *ui_bar(lv_obj_t *parent, int w, int h, uint32_t color);
void ui_bar_set(lv_obj_t *bar, float pct, uint32_t color);

/* Color for a utilization percentage: green < warn <= amber < crit <= red. */
uint32_t ui_level_color(float pct, float warn, float crit);
/* "2h 14m" / "5d 3h" / "45s". */
void ui_fmt_dur(char *buf, size_t n, int32_t s);
/* Blend two 0xRRGGBB colors, mix 0..255 toward b. */
uint32_t ui_mix(uint32_t a, uint32_t b, uint8_t mix);
