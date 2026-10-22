/* HUD page — your computer (left) and an optional server (right). Without
 * server data the computer gets the whole page. Each column's header:
 *
 *   [icon] Name                      CPU model
 *   42%  CPU 52°                     GPU model
 *
 * Names and models come from the snapshot (pc / lab: name, cpu_model,
 * gpu_model). The desktop app summarizes the server's containers so the
 * panel never parses a big container list; restarts are hold-to-confirm. A
 * faint footer shows the panel's own address while it is on Wi-Fi.
 *
 * Every setter here is change-aware: a snapshot arrives twice a second and
 * an unconditional set (text, colour, hidden flag) redraws, and for the
 * one-line "dots" labels even re-lays out, on every one of them. */
#include "net.h"
#include "store.h"
#include "ui_pages.h"
#include "ui_theme.h"

#include <Arduino.h>
#include <stdio.h>
#include <string.h>

#define COL_W 376
#define WIDE_W 768
#define MODEL_GAP 12   /* name <-> CPU model */
#define CAPTION_END 240 /* the big % + "CPU 52°" end here; the GPU model starts after */
#define HOLD_MS 1200
#define SPARK_POINTS 90

struct Row {
  lv_obj_t *name, *bar, *val;
};

/* Text last set on a one-line label: LVGL writes the "..." into the label's
 * own text, so comparing against lv_label_get_text() would never match. */
struct Shown {
  char txt[64];
};

struct Column {
  lv_obj_t *root, *title, *cpu, *gpu, *big, *big_cap, *chart;
  lv_chart_series_t *ser;
  Row rows[4];
  lv_obj_t *foot1, *foot2;
  int w;              /* current width */
  int cpu_w, gpu_w;   /* laid-out widths of the model labels */
  Shown title_s, cpu_s, gpu_s;
};

static Column s_pc, s_lab;
static lv_obj_t *s_sep, *s_footer;
static Shown s_footer_s, s_ok_s;
static int s_wide = -1; /* layout shown: 1 = computer only, 0 = two columns */
static char s_pc_name[40] = "Your PC";
static char s_lab_name[32] = "Server";
/* Title font: the heading face + Montserrat for glyphs it lacks (names are
 * free text from the host). */
static lv_font_t s_title_font;
static lv_obj_t *s_bad_box;
static lv_obj_t *s_bad_rows[3];
static lv_obj_t *s_bad_name[3], *s_bad_state[3];
static LabItem s_bad_items[3];
static uint8_t s_n_bad_shown = 0;
static lv_obj_t *s_ok_row, *s_ok_lbl;

/* restart confirm modal */
static lv_obj_t *s_modal, *s_modal_title, *s_modal_sub, *s_hold, *s_hold_fill, *s_hold_lbl;
static char s_modal_id[16];
static uint32_t s_hold_start = 0;
static bool s_hold_done = false;

/* ---- change-aware helpers -------------------------------------------------- */
static bool set_shown(lv_obj_t *label, Shown &s, const char *txt) {
  if (!txt) txt = "";
  if (!strncmp(s.txt, txt, sizeof s.txt - 1) && strlen(txt) < sizeof s.txt) return false;
  snprintf(s.txt, sizeof s.txt, "%s", txt);
  lv_label_set_text(label, txt);
  return true;
}

static void show(lv_obj_t *obj, bool on) {
  bool hidden = lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN);
  if (on && hidden) lv_obj_remove_flag(obj, LV_OBJ_FLAG_HIDDEN);
  else if (!on && !hidden) lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
}

static void set_width(lv_obj_t *obj, int32_t w) {
  if (lv_obj_get_style_width(obj, LV_PART_MAIN) != w) lv_obj_set_width(obj, w);
}

static void set_pos(lv_obj_t *obj, int32_t x, int32_t y) {
  if (lv_obj_get_style_x(obj, LV_PART_MAIN) != x || lv_obj_get_style_y(obj, LV_PART_MAIN) != y)
    lv_obj_set_pos(obj, x, y);
}

static int text_w(const char *txt, const lv_font_t *font) {
  if (!txt || !*txt) return 0;
  lv_point_t p;
  lv_text_get_size(&p, txt, font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
  return p.x + 2; /* rounding slack: never trigger the dots on a text that fits */
}

/* ---- build ------------------------------------------------------------------ */
static void build_row(lv_obj_t *col, Row &r, int y, const char *name) {
  r.name = ui_label(col, &lv_font_montserrat_14, COL_TEXT_DIM, name);
  lv_obj_set_style_text_letter_space(r.name, 1, 0);
  lv_obj_set_pos(r.name, 0, y);
  r.bar = ui_bar(col, 184, 6, COL_GREEN);
  lv_obj_set_pos(r.bar, 84, y + 6);
  r.val = ui_label(col, &lv_font_montserrat_16, COL_TEXT, "--");
  lv_obj_set_width(r.val, 104);
  lv_obj_set_style_text_align(r.val, LV_TEXT_ALIGN_RIGHT, 0);
  lv_obj_set_pos(r.val, COL_W - 104, y - 1);
}

static lv_obj_t *model_label(lv_obj_t *parent) {
  lv_obj_t *l = ui_label(parent, &lv_font_montserrat_16, COL_TEXT_DIM, "");
  lv_obj_set_width(l, 0);
  lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_RIGHT, 0);
  ui_one_line(l);
  return l;
}

static void build_column(lv_obj_t *tile, Column &c, int x, const char *ico, const char *title) {
  c.root = ui_box(tile);
  lv_obj_set_pos(c.root, x, 10);
  lv_obj_set_size(c.root, COL_W, CONTENT_H - 20);
  c.w = COL_W;
  c.cpu_w = c.gpu_w = -1;

  lv_obj_t *i = ui_label(c.root, &font_icons_20, COL_CLAY, icon(ico));
  lv_obj_set_pos(i, 0, 8);
  c.title = ui_label(c.root, &s_title_font, COL_TEXT, "");
  lv_obj_set_pos(c.title, 30, 0);
  lv_obj_set_width(c.title, COL_W - 30);
  ui_one_line(c.title);
  set_shown(c.title, c.title_s, title);
  c.cpu = model_label(c.root); /* row 1, right: CPU model */
  c.gpu = model_label(c.root); /* row 2, right: GPU model */

  c.big = ui_label(c.root, &font_serif_56, COL_TEXT, "--");
  lv_obj_set_pos(c.big, 0, 36);
  c.big_cap = ui_label(c.root, &lv_font_montserrat_14, COL_TEXT_DIM, "CPU");
  lv_obj_set_style_text_letter_space(c.big_cap, 2, 0);
  lv_obj_set_pos(c.big_cap, 150, 70);

  c.chart = lv_chart_create(c.root);
  lv_obj_remove_style_all(c.chart);
  lv_obj_set_size(c.chart, COL_W, 54);
  lv_obj_set_pos(c.chart, 0, 104);
  lv_chart_set_type(c.chart, LV_CHART_TYPE_LINE);
  lv_chart_set_point_count(c.chart, SPARK_POINTS);
  lv_chart_set_axis_range(c.chart, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
  lv_chart_set_div_line_count(c.chart, 0, 0);
  lv_chart_set_update_mode(c.chart, LV_CHART_UPDATE_MODE_SHIFT);
  lv_obj_set_style_size(c.chart, 0, 0, LV_PART_INDICATOR);
  lv_obj_set_style_line_width(c.chart, 2, LV_PART_ITEMS);
  lv_obj_set_style_bg_color(c.chart, C(COL_SURFACE), 0);
  lv_obj_set_style_bg_opa(c.chart, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(c.chart, 8, 0);
  c.ser = lv_chart_add_series(c.chart, C(COL_CLAY), LV_CHART_AXIS_PRIMARY_Y);
  lv_chart_set_all_values(c.chart, c.ser, LV_CHART_POINT_NONE);
}

/* Header geometry for the column's width and the current model texts: the
 * name takes what the CPU model leaves; the GPU model sits right of the big
 * CPU figure. Only touches objects whose geometry actually changes. */
static void layout_header(Column &c) {
  int cpu_w = text_w(c.cpu_s.txt, &lv_font_montserrat_16);
  if (cpu_w > c.w / 2) cpu_w = c.w / 2;
  int gpu_w = text_w(c.gpu_s.txt, &lv_font_montserrat_16);
  if (gpu_w > c.w - CAPTION_END) gpu_w = c.w - CAPTION_END;
  set_width(c.title, c.w - 30 - (cpu_w ? cpu_w + MODEL_GAP : 0));
  if (cpu_w != c.cpu_w) {
    set_width(c.cpu, cpu_w);
    c.cpu_w = cpu_w;
  }
  set_pos(c.cpu, c.w - cpu_w, 9);
  if (gpu_w != c.gpu_w) {
    set_width(c.gpu, gpu_w);
    c.gpu_w = gpu_w;
  }
  set_pos(c.gpu, c.w - gpu_w, 67);
}

static void set_models(Column &c, const char *cpu, const char *gpu, uint32_t color) {
  bool changed = set_shown(c.cpu, c.cpu_s, cpu);
  changed = set_shown(c.gpu, c.gpu_s, gpu) || changed;
  ui_set_text_color(c.cpu, color);
  if (changed) layout_header(c);
}

/* Column width w: the header, rows and sparkline stretch with it. */
static void layout_column(Column &c, int w) {
  c.w = w;
  set_width(c.root, w);
  set_width(c.chart, w);
  for (Row &r : c.rows) {
    set_width(r.bar, w - 84 - 108);
    set_pos(r.val, w - 104, lv_obj_get_style_y(r.val, LV_PART_MAIN));
  }
  layout_header(c);
}

static void set_wide(bool wide) {
  if ((int)wide == s_wide) return;
  s_wide = wide ? 1 : 0;
  layout_column(s_pc, wide ? WIDE_W : COL_W);
  show(s_sep, !wide);
  show(s_lab.root, !wide);
}

/* ---- restart modal ------------------------------------------------------- */
static void modal_close() {
  lv_obj_add_flag(s_modal, LV_OBJ_FLAG_HIDDEN);
  s_hold_start = 0;
  s_hold_done = false;
}

static void on_hold(lv_event_t *e) {
  lv_event_code_t code = lv_event_get_code(e);
  if (code == LV_EVENT_PRESSED) {
    s_hold_start = millis();
    s_hold_done = false;
  } else if (code == LV_EVENT_PRESSING && s_hold_start && !s_hold_done) {
    uint32_t held = millis() - s_hold_start;
    int w = (int)(440 * (held > HOLD_MS ? HOLD_MS : held) / HOLD_MS);
    lv_obj_set_width(s_hold_fill, w);
    if (held >= HOLD_MS) {
      s_hold_done = true;
      char arg[48];
      snprintf(arg, sizeof arg, "restart:%s", s_modal_id);
      store_push_event("cmd", s_modal_id, arg);
      ui_set_text(s_hold_lbl, "RESTART SENT");
    }
  } else if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
    if (s_hold_done) {
      modal_close();
    } else {
      s_hold_start = 0;
      lv_obj_set_width(s_hold_fill, 0);
    }
  }
}

static void on_cancel(lv_event_t *) { modal_close(); }

static void build_modal() {
  s_modal = ui_box(lv_layer_top());
  lv_obj_set_size(s_modal, 800, 480);
  lv_obj_set_style_bg_color(s_modal, C(0x000000), 0);
  lv_obj_set_style_bg_opa(s_modal, LV_OPA_COVER, 0);
  lv_obj_add_flag(s_modal, LV_OBJ_FLAG_CLICKABLE); /* eat taps behind it */

  lv_obj_t *card = ui_card(s_modal, COL_SURFACE, 20);
  lv_obj_set_size(card, 520, 280);
  lv_obj_center(card);
  lv_obj_set_style_border_width(card, 2, 0);
  lv_obj_set_style_border_color(card, C(COL_AMBER), 0);

  s_modal_title = ui_label(card, &font_head_28, COL_TEXT, "");
  lv_obj_set_width(s_modal_title, 460);
  ui_one_line(s_modal_title);
  lv_obj_set_pos(s_modal_title, 30, 26);
  s_modal_sub = ui_label(card, &lv_font_montserrat_16, COL_TEXT_DIM, "");
  lv_obj_set_pos(s_modal_sub, 30, 68);

  s_hold = ui_card(card, COL_SURFACE2, 14);
  lv_obj_set_size(s_hold, 440, 80);
  lv_obj_set_pos(s_hold, 40, 112);
  lv_obj_add_flag(s_hold, LV_OBJ_FLAG_CLICKABLE);
  s_hold_fill = ui_card(s_hold, COL_AMBER, 14);
  lv_obj_set_size(s_hold_fill, 0, 80);
  s_hold_lbl = ui_label(s_hold, &lv_font_montserrat_20, COL_TEXT, "HOLD TO RESTART");
  lv_obj_set_style_text_letter_space(s_hold_lbl, 2, 0);
  lv_obj_center(s_hold_lbl);
  lv_obj_add_event_cb(s_hold, on_hold, LV_EVENT_ALL, nullptr);

  lv_obj_t *cancel = ui_box(card);
  lv_obj_set_size(cancel, 200, 50);
  lv_obj_set_pos(cancel, 160, 210);
  lv_obj_add_flag(cancel, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_t *cl = ui_label(cancel, &lv_font_montserrat_18, COL_TEXT_DIM, "Cancel");
  lv_obj_center(cl);
  lv_obj_add_event_cb(cancel, on_cancel, LV_EVENT_CLICKED, nullptr);

  lv_obj_add_flag(s_modal, LV_OBJ_FLAG_HIDDEN);
}

static void on_bad_row(lv_event_t *e) {
  int i = (int)(intptr_t)lv_event_get_user_data(e);
  if (i >= s_n_bad_shown) return;
  const LabItem &it = s_bad_items[i];
  strncpy(s_modal_id, it.id, sizeof s_modal_id - 1);
  s_modal_id[sizeof s_modal_id - 1] = '\0';
  char b[64];
  snprintf(b, sizeof b, "Restart %s?", it.name);
  ui_set_text(s_modal_title, b);
  snprintf(b, sizeof b, "%s  \xE2\x80\xA2  %s%s%s", s_lab_name, it.state, it.health[0] ? " / " : "", it.health);
  ui_set_text(s_modal_sub, b);
  ui_set_text(s_hold_lbl, "HOLD TO RESTART");
  lv_obj_set_width(s_hold_fill, 0);
  lv_obj_remove_flag(s_modal, LV_OBJ_FLAG_HIDDEN);
}

void hud_page_build(lv_obj_t *tile) {
  s_title_font = font_head_28;
  s_title_font.fallback = &lv_font_montserrat_20;
  build_column(tile, s_pc, 16, "chip", s_pc_name);
  build_row(s_pc.root, s_pc.rows[0], 172, "RAM");
  build_row(s_pc.root, s_pc.rows[1], 204, "GPU");
  build_row(s_pc.root, s_pc.rows[2], 236, "VRAM");
  build_row(s_pc.root, s_pc.rows[3], 268, "DISK");
  s_pc.foot1 = ui_label(s_pc.root, &font_mono_16, COL_TEXT_DIM, "");
  lv_obj_set_pos(s_pc.foot1, 0, 308);
  s_pc.foot2 = ui_label(s_pc.root, &font_mono_16, COL_TEXT_DIM, "");
  lv_obj_set_pos(s_pc.foot2, 0, 334);

  s_sep = ui_card(tile, COL_LINE, 0);
  lv_obj_set_size(s_sep, 1, CONTENT_H - 40);
  lv_obj_set_pos(s_sep, 400, 20);

  build_column(tile, s_lab, 408, "server", s_lab_name);
  build_row(s_lab.root, s_lab.rows[0], 172, "RAM");
  build_row(s_lab.root, s_lab.rows[1], 204, "GPU");
  build_row(s_lab.root, s_lab.rows[2], 236, "DISK");
  build_row(s_lab.root, s_lab.rows[3], 268, "DOCKER");
  s_lab.foot1 = ui_label(s_lab.root, &lv_font_montserrat_14, COL_TEXT_DIM, "");
  lv_obj_set_pos(s_lab.foot1, 0, 296);
  s_lab.foot2 = nullptr;
  layout_header(s_pc);
  layout_header(s_lab);

  s_bad_box = ui_box(s_lab.root);
  lv_obj_set_pos(s_bad_box, 0, 318);
  lv_obj_set_size(s_bad_box, COL_W, 94);
  for (int i = 0; i < 3; i++) {
    lv_obj_t *r = ui_card(s_bad_box, COL_SURFACE, 8);
    lv_obj_set_size(r, COL_W, 28);
    lv_obj_set_pos(r, 0, i * 32);
    lv_obj_add_flag(r, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_color(r, C(COL_SURFACE2), LV_STATE_PRESSED);
    s_bad_name[i] = ui_label(r, &lv_font_montserrat_14, COL_TEXT, "");
    lv_obj_align(s_bad_name[i], LV_ALIGN_LEFT_MID, 10, 0);
    s_bad_state[i] = ui_label(r, &lv_font_montserrat_12, COL_RED, "");
    lv_obj_align(s_bad_state[i], LV_ALIGN_RIGHT_MID, -10, 0);
    lv_obj_add_event_cb(r, on_bad_row, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    lv_obj_add_flag(r, LV_OBJ_FLAG_HIDDEN);
    s_bad_rows[i] = r;
  }
  s_ok_row = ui_box(s_bad_box);
  lv_obj_set_size(s_ok_row, COL_W, 28);
  lv_obj_t *ok_ico = ui_label(s_ok_row, &font_icons_20, COL_GREEN, icon("check"));
  lv_obj_align(ok_ico, LV_ALIGN_LEFT_MID, 0, 0);
  s_ok_lbl = ui_label(s_ok_row, &lv_font_montserrat_14, COL_TEXT_DIM, "");
  lv_obj_set_width(s_ok_lbl, COL_W - 30);
  ui_one_line(s_ok_lbl);
  lv_obj_align(s_ok_lbl, LV_ALIGN_LEFT_MID, 28, 0);
  lv_obj_add_flag(s_ok_row, LV_OBJ_FLAG_HIDDEN);

  s_footer = ui_label(tile, &lv_font_montserrat_12, COL_TEXT_FAINT, "");
  lv_obj_set_width(s_footer, COL_W);
  ui_one_line(s_footer);
  lv_obj_set_pos(s_footer, 16, CONTENT_H - 20);
  lv_obj_add_flag(s_footer, LV_OBJ_FLAG_HIDDEN);
  build_modal();
  set_wide(true);
}

/* ---- update -------------------------------------------------------------------- */
/* "192.168.1.50  *  pandadeck.local" while on Wi-Fi, so the address can be
 * typed into the app when USB detection fails. */
static void update_footer() {
  char ip[16], b[64];
  bool up = net_ip(ip, sizeof ip);
  if (up) {
    snprintf(b, sizeof b, "%s  \xE2\x80\xA2  %s.local", ip, net_hostname());
    set_shown(s_footer, s_footer_s, b);
  }
  show(s_footer, up);
}

static void set_row(Row &r, float pct, const char *txt, uint32_t col) {
  ui_bar_set(r.bar, pct < 0 ? 0 : pct, pct < 0 ? COL_TEXT_FAINT : col);
  ui_set_text(r.val, txt);
}

static void fmt_pct(char *b, size_t n, float v) {
  if (v < 0) snprintf(b, n, "--");
  else snprintf(b, n, "%d%%", (int)(v + 0.5f));
}

static void update_pc(const PcInfo &p, bool agent_ok) {
  char b[64];
  bool ok = agent_ok && p.ok;
  if (ok && p.name[0]) snprintf(s_pc_name, sizeof s_pc_name, "%s", p.name);
  set_shown(s_pc.title, s_pc.title_s, s_pc_name);
  if (ok) set_models(s_pc, p.cpu_model, p.gpu_model, COL_TEXT_DIM);
  else set_models(s_pc, "offline", "", COL_TEXT_FAINT);
  fmt_pct(b, sizeof b, ok ? p.cpu : -1);
  ui_set_text(s_pc.big, b);
  if (ok && p.cpu_t > 0) snprintf(b, sizeof b, "CPU  %d\xC2\xB0", (int)p.cpu_t);
  else snprintf(b, sizeof b, "CPU");
  ui_set_text(s_pc.big_cap, b);

  fmt_pct(b, sizeof b, ok ? p.ram : -1);
  set_row(s_pc.rows[0], ok ? p.ram : -1, b, ui_level_color(p.ram, 75, 90));
  if (ok && p.gpu >= 0) snprintf(b, sizeof b, "%d%% %d\xC2\xB0", (int)p.gpu, (int)p.gpu_t);
  else snprintf(b, sizeof b, "--");
  set_row(s_pc.rows[1], ok ? p.gpu : -1, b, ui_level_color(p.gpu_t, 75, 85));
  fmt_pct(b, sizeof b, ok ? p.vram : -1);
  set_row(s_pc.rows[2], ok ? p.vram : -1, b, ui_level_color(p.vram, 80, 95));
  fmt_pct(b, sizeof b, ok ? p.disk : -1);
  set_row(s_pc.rows[3], ok ? p.disk : -1, b, ui_level_color(p.disk, 85, 95));

  if (ok) {
    snprintf(b, sizeof b, "\xE2\x86\x93 %.1f  \xE2\x86\x91 %.1f Mb/s", p.down_mbps < 0 ? 0 : p.down_mbps,
             p.up_mbps < 0 ? 0 : p.up_mbps);
    ui_set_text(s_pc.foot1, b);
    if (p.gpu_w >= 0) snprintf(b, sizeof b, "GPU %.0f W", p.gpu_w);
    else b[0] = '\0';
    ui_set_text(s_pc.foot2, b);
  } else {
    ui_set_text(s_pc.foot1, "");
    ui_set_text(s_pc.foot2, "");
  }
}

static void update_bad_rows(const LabInfo &l, bool ok) {
  s_n_bad_shown = ok ? (l.n_bad > 3 ? 3 : l.n_bad) : 0;
  for (int i = 0; i < 3; i++) {
    show(s_bad_rows[i], i < s_n_bad_shown);
    if (i >= s_n_bad_shown) continue;
    s_bad_items[i] = l.bad[i];
    ui_set_text(s_bad_name[i], l.bad[i].name);
    const char *st = l.bad[i].health[0] ? l.bad[i].health : l.bad[i].state;
    ui_set_text(s_bad_state[i], st);
    bool warn = !strcmp(st, "restarting") || !strcmp(st, "starting");
    ui_set_text_color(s_bad_state[i], warn ? COL_AMBER : COL_RED);
  }
  bool all_ok = ok && s_n_bad_shown == 0;
  if (all_ok) {
    char t[96];
    if (l.n_top > 0) {
      snprintf(t, sizeof t, "All healthy  \xE2\x80\xA2  busiest: %s %.0f%%", l.top[0].name, l.top[0].cpu);
    } else {
      snprintf(t, sizeof t, "All containers healthy");
    }
    set_shown(s_ok_lbl, s_ok_s, t);
  }
  show(s_ok_row, all_ok);
}

static void update_lab(const LabInfo &l, bool agent_ok) {
  char b[80];
  bool ok = agent_ok && l.ok;
  if (ok && l.name[0]) snprintf(s_lab_name, sizeof s_lab_name, "%s", l.name);
  set_shown(s_lab.title, s_lab.title_s, s_lab_name);
  set_models(s_lab, ok ? l.cpu_model : "", ok ? l.gpu_model : "", COL_TEXT_DIM);
  fmt_pct(b, sizeof b, ok ? l.cpu : -1);
  ui_set_text(s_lab.big, b);
  if (ok && l.temp > 0) snprintf(b, sizeof b, "CPU  %d\xC2\xB0", (int)l.temp);
  else snprintf(b, sizeof b, "CPU");
  ui_set_text(s_lab.big_cap, b);

  fmt_pct(b, sizeof b, ok ? l.ram : -1);
  set_row(s_lab.rows[0], ok ? l.ram : -1, b, ui_level_color(l.ram, 80, 92));
  if (ok && l.gpu >= 0) snprintf(b, sizeof b, "%d%% %d\xC2\xB0", (int)l.gpu, (int)l.gpu_t);
  else snprintf(b, sizeof b, "--");
  set_row(s_lab.rows[1], ok ? l.gpu : -1, b, ui_level_color(l.gpu_t, 75, 85));
  fmt_pct(b, sizeof b, ok ? l.disk : -1);
  set_row(s_lab.rows[2], ok ? l.disk : -1, b, ui_level_color(l.disk, 85, 95));
  /* DOCKER: bar = share of containers running, value = running / total */
  float up_pct = (ok && l.total > 0) ? 100.0f * l.run / l.total : -1;
  if (ok && l.total > 0) snprintf(b, sizeof b, "%d/%d up", l.run, l.total);
  else snprintf(b, sizeof b, "--");
  set_row(s_lab.rows[3], up_pct, b, l.n_bad ? COL_AMBER : COL_GREEN);

  b[0] = '\0';
  if (ok && l.backup_h >= 0) snprintf(b, sizeof b, "Backup %.1fh ago", l.backup_h);
  if (ok && l.healthy >= 0) {
    size_t n = strlen(b);
    snprintf(b + n, sizeof b - n, "%s%d healthy", n ? "  \xE2\x80\xA2  " : "", l.healthy);
  }
  ui_set_text(s_lab.foot1, b);
  update_bad_rows(l, ok);
}

void hud_page_update(const Model &m, bool agent_ok) {
  set_wide(!(agent_ok && m.lab.ok));
  update_pc(m.pc, agent_ok);
  update_lab(m.lab, agent_ok);
  update_footer();
}

void hud_page_sample(const Model &m) {
  update_footer();
  bool pc_ok = m.pc.ok && m.pc.cpu >= 0;
  lv_chart_set_next_value(s_pc.chart, s_pc.ser, pc_ok ? (int32_t)m.pc.cpu : LV_CHART_POINT_NONE);
  if (s_wide == 1) return; /* server column hidden: don't redraw it */
  bool lab_ok = m.lab.ok && m.lab.cpu >= 0;
  lv_chart_set_next_value(s_lab.chart, s_lab.ser, lab_ok ? (int32_t)m.lab.cpu : LV_CHART_POINT_NONE);
}
