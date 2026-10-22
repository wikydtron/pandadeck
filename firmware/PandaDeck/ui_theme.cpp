#include "ui_theme.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

struct IconDef {
  const char *name;
  uint32_t cp;
  char utf8[4];
};

#include "icon_map.h"

static void encode_utf8(uint32_t cp, char out[4]) {
  /* All icons are in the BMP private-use area: always 3 bytes. */
  out[0] = (char)(0xE0 | (cp >> 12));
  out[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
  out[2] = (char)(0x80 | (cp & 0x3F));
  out[3] = '\0';
}

const char *icon(const char *name) {
  if (!name || !*name) return "";
  for (IconDef &d : ICONS) {
    if (strcmp(d.name, name) == 0) {
      if (!d.utf8[0]) encode_utf8(d.cp, d.utf8);
      return d.utf8;
    }
  }
  return "";
}

lv_obj_t *ui_box(lv_obj_t *parent) {
  lv_obj_t *o = lv_obj_create(parent);
  lv_obj_remove_style_all(o);
  lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
  return o;
}

lv_obj_t *ui_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *txt) {
  lv_obj_t *l = lv_label_create(parent);
  lv_obj_set_style_text_font(l, font, 0);
  lv_obj_set_style_text_color(l, C(color), 0);
  lv_label_set_text(l, txt ? txt : "");
  return l;
}

/* LVGL rewrites a "..."-truncated label's own text, so comparing against the
 * label can't spot an unchanged value: remember a hash of what was requested
 * (object user data is otherwise unused on labels). */
static uintptr_t text_hash(const char *s) {
  uint32_t h = 2166136261u;
  for (; *s; s++) h = (h ^ (uint8_t)*s) * 16777619u;
  return (uintptr_t)h | 1u; /* never 0 = "nothing remembered" */
}

void ui_set_text(lv_obj_t *label, const char *txt) {
  if (!txt) txt = "";
  uintptr_t h = text_hash(txt);
  const char *cur = lv_label_get_text(label);
  if ((cur && strcmp(cur, txt) == 0) || (uintptr_t)lv_obj_get_user_data(label) == h) return;
  lv_label_set_text(label, txt);
  lv_obj_set_user_data(label, (void *)h);
}

void ui_set_text_color(lv_obj_t *obj, uint32_t color) {
  lv_color_t want = C(color);
  if (lv_color_eq(lv_obj_get_style_text_color(obj, LV_PART_MAIN), want)) return;
  lv_obj_set_style_text_color(obj, want, 0);
}

void ui_one_line(lv_obj_t *label) {
  const lv_font_t *f = lv_obj_get_style_text_font(label, LV_PART_MAIN);
  lv_obj_set_height(label, lv_font_get_line_height(f));
  lv_label_set_long_mode(label, LV_LABEL_LONG_MODE_DOTS);
}

lv_obj_t *ui_card(lv_obj_t *parent, uint32_t bg, int radius) {
  lv_obj_t *o = ui_box(parent);
  lv_obj_set_style_bg_color(o, C(bg), 0);
  lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(o, radius, 0);
  return o;
}

lv_obj_t *ui_bar(lv_obj_t *parent, int w, int h, uint32_t color) {
  lv_obj_t *b = lv_bar_create(parent);
  lv_obj_remove_style_all(b);
  lv_obj_set_size(b, w, h);
  lv_bar_set_range(b, 0, 1000);
  lv_obj_set_style_bg_color(b, C(COL_SURFACE2), LV_PART_MAIN);
  lv_obj_set_style_bg_opa(b, LV_OPA_COVER, LV_PART_MAIN);
  lv_obj_set_style_radius(b, h / 2, LV_PART_MAIN);
  lv_obj_set_style_bg_color(b, C(color), LV_PART_INDICATOR);
  lv_obj_set_style_bg_opa(b, LV_OPA_COVER, LV_PART_INDICATOR);
  lv_obj_set_style_radius(b, h / 2, LV_PART_INDICATOR);
  lv_obj_set_style_anim_duration(b, 400, 0);
  return b;
}

void ui_bar_set(lv_obj_t *bar, float pct, uint32_t color) {
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  /* Half-percent resolution and no animation: an animated bar redraws every
   * frame for 400 ms on each update, which kept the whole UI busy. */
  int32_t v = (int32_t)(pct * 2) * 5;
  if (lv_bar_get_value(bar) != v) lv_bar_set_value(bar, v, LV_ANIM_OFF);
  lv_color_t c = C(color);
  if (!lv_color_eq(lv_obj_get_style_bg_color(bar, LV_PART_INDICATOR), c))
    lv_obj_set_style_bg_color(bar, c, LV_PART_INDICATOR);
}

uint32_t ui_level_color(float pct, float warn, float crit) {
  if (pct >= crit) return COL_RED;
  if (pct >= warn) return COL_AMBER;
  return COL_GREEN;
}

void ui_fmt_dur(char *buf, size_t n, int32_t s) {
  if (s < 0) {
    snprintf(buf, n, "--");
  } else if (s < 60) {
    snprintf(buf, n, "%lds", (long)s);
  } else if (s < 3600) {
    snprintf(buf, n, "%ldm", (long)(s / 60));
  } else if (s < 86400) {
    snprintf(buf, n, "%ldh %ldm", (long)(s / 3600), (long)((s % 3600) / 60));
  } else {
    snprintf(buf, n, "%ldd %ldh", (long)(s / 86400), (long)((s % 86400) / 3600));
  }
}

uint32_t ui_mix(uint32_t a, uint32_t b, uint8_t mix) {
  uint32_t out = 0;
  for (int sh = 0; sh <= 16; sh += 8) {
    uint32_t ca = (a >> sh) & 0xFF, cb = (b >> sh) & 0xFF;
    out |= ((ca * (255 - mix) + cb * mix) / 255) << sh;
  }
  return out;
}
