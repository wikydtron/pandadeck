/* DECK page — touch Stream Deck + app launcher.
 * keys:/media: actions go straight out as USB HID from the panel (zero
 * latency, no PC software); agent: actions (launch an app, mic, ...) are
 * queued for the PC agent's next sync.
 * Pages are 4 columns (12 big buttons) or 6 columns (18 app tiles); a button
 * with `img` shows a real app icon pushed by the agent (icon_store). */
#include "deck_action.h"
#include "hid.h"
#include "icon_store.h"
#include "store.h"
#include "ui_pages.h"
#include "ui_theme.h"

#include <Arduino.h>
#include <stdio.h>
#include <string.h>

#define GRID_ROWS 3
#define GRID_W 768
#define BTN_H 113
#define GAP 12
#define MAX_BTNS MODEL_MAX_DECK_BUTTONS

struct Btn {
  lv_obj_t *root, *plate, *ico, *img, *lbl; /* plate: the coloured square in icons style */
  uint32_t color;
};

#define PLATE_PX_LABELS 80   /* icons style: glyph plate size with names under it */
#define PLATE_PX_ALONE 96    /* ... and without names */

static DeckLayout s_layout;
static int s_page = 0;
static lv_obj_t *s_tabs_row;
static lv_obj_t *s_tabs[MODEL_MAX_DECK_PAGES];
static lv_obj_t *s_grid;
static Btn s_btns[MAX_BTNS];
static lv_obj_t *s_usb_note;
static bool s_mic_muted = false;
static bool s_playing = false;
static bool s_built = false;
static uint32_t s_icons_rev = 0;

static uint32_t page_color(int p) {
  uint32_t c = s_layout.page[p].color;
  return c ? c : COL_CLAY;
}

static void flash_anim_cb(void *obj, int32_t v) {
  lv_obj_set_style_border_opa((lv_obj_t *)obj, (lv_opa_t)v, 0);
}

static void flash(lv_obj_t *o, uint32_t color) {
  lv_obj_set_style_border_color(o, C(color), 0);
  lv_obj_set_style_border_width(o, 3, 0);
  lv_anim_t a;
  lv_anim_init(&a);
  lv_anim_set_var(&a, o);
  lv_anim_set_values(&a, LV_OPA_COVER, LV_OPA_TRANSP);
  lv_anim_set_duration(&a, 380);
  lv_anim_set_exec_cb(&a, flash_anim_cb);
  lv_anim_start(&a);
}

static void apply_media_state(int idx, const DeckButton &b);

static void on_btn(lv_event_t *e) {
  int idx = (int)(intptr_t)lv_event_get_user_data(e);
  if (s_page >= s_layout.n_page || idx >= s_layout.page[s_page].n_btn) return;
  const DeckButton &b = s_layout.page[s_page].btn[idx];
  DeckAction a;
  if (!deck_parse_action(b.action, &a)) return;
  if (a.type == DECK_AGENT) {
    /* Don't queue PC commands for an agent that isn't there: a mic toggle or
     * app launch replayed minutes later is worse than nothing. */
    if (store_ms_since_sync() > 4000) {
      flash(s_btns[idx].root, COL_RED);
      return;
    }
    store_push_event("cmd", "", a.agent);
  } else {
    hid_fire(a);
    if (!strcmp(b.action, "media:playpause")) {
      music_toggle_optimistic();
      s_playing = music_playing();
      apply_media_state(idx, b);
    }
  }
  flash(s_btns[idx].root, s_btns[idx].color);
}

static void style_tab(int i, bool active) {
  uint32_t col = page_color(i);
  lv_obj_set_style_bg_color(s_tabs[i], C(active ? ui_mix(COL_BG, col, 60) : COL_SURFACE), 0);
  lv_obj_t *l = lv_obj_get_child(s_tabs[i], 0);
  lv_obj_set_style_text_color(l, C(active ? col : COL_TEXT_DIM), 0);
}

static void render_buttons();

static void on_tab(lv_event_t *e) {
  int idx = (int)(intptr_t)lv_event_get_user_data(e);
  if (idx == s_page || idx >= s_layout.n_page) return;
  s_page = idx;
  for (int i = 0; i < s_layout.n_page; i++) style_tab(i, i == s_page);
  render_buttons();
}

static void apply_mic_state(int idx, const DeckButton &b) {
  if (strcmp(b.action, "agent:mic") != 0) return;
  ui_set_text(s_btns[idx].ico, icon(s_mic_muted ? "mic_off" : "mic"));
  if (s_layout.style == DECK_ICONS) /* white glyph on a plate that turns red */
    lv_obj_set_style_bg_color(s_btns[idx].plate, C(s_mic_muted ? COL_RED : s_btns[idx].color), 0);
  else
    ui_set_text_color(s_btns[idx].ico, s_mic_muted ? COL_RED : s_btns[idx].color);
  ui_set_text(s_btns[idx].lbl, s_mic_muted ? "Muted" : b.label);
}

/* The play/pause key shows what a tap will do, from the PC's media session. */
static void apply_media_state(int idx, const DeckButton &b) {
  if (strcmp(b.action, "media:playpause") != 0) return;
  ui_set_text(s_btns[idx].ico, icon(s_playing ? "pause" : "play"));
  ui_set_text(s_btns[idx].lbl, s_playing ? "Pause" : "Play");
}

static void layout_button(Btn &w, int i, int cols) {
  const int bw = (GRID_W - (cols - 1) * GAP) / cols;
  lv_obj_set_size(w.root, bw, BTN_H);
  lv_obj_set_pos(w.root, (i % cols) * (bw + GAP), (i / cols) * (BTN_H + GAP));
  lv_obj_set_width(w.lbl, bw - 12);
  lv_obj_set_style_text_font(w.lbl, cols >= 6 ? &lv_font_montserrat_14 : &lv_font_montserrat_18, 0);
  ui_one_line(w.lbl);
  lv_obj_align(w.lbl, LV_ALIGN_BOTTOM_MID, 0, cols >= 6 ? -10 : -12);
}

/* Icons style: no card -- the app icon (sent at a larger size) or a coloured
 * plate with a white glyph IS the button; optional name underneath. */
static void style_icons_button(Btn &w, bool has_img, bool labels) {
  lv_obj_set_style_bg_opa(w.root, LV_OPA_TRANSP, 0);
  lv_obj_set_style_bg_opa(w.root, LV_OPA_40, LV_STATE_PRESSED);
  const int dy = labels ? -9 : 0;
  if (has_img) {
    lv_obj_add_flag(w.plate, LV_OBJ_FLAG_HIDDEN);
    lv_obj_align(w.img, LV_ALIGN_CENTER, 0, dy);
  } else {
    const int px = labels ? PLATE_PX_LABELS : PLATE_PX_ALONE;
    lv_obj_remove_flag(w.plate, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_size(w.plate, px, px);
    lv_obj_set_style_radius(w.plate, px * 22 / 100, 0);
    lv_obj_set_style_bg_color(w.plate, C(w.color), 0);
    lv_obj_align(w.plate, LV_ALIGN_CENTER, 0, dy);
    ui_set_text_color(w.ico, COL_TEXT);
    lv_obj_align_to(w.ico, w.plate, LV_ALIGN_CENTER, 0, 0);
  }
  if (labels) {
    lv_obj_remove_flag(w.lbl, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_text_font(w.lbl, &lv_font_montserrat_14, 0);
    ui_set_text_color(w.lbl, COL_TEXT_DIM);
    lv_obj_align(w.lbl, LV_ALIGN_BOTTOM_MID, 0, 0);
  } else {
    lv_obj_add_flag(w.lbl, LV_OBJ_FLAG_HIDDEN);
  }
}

static void style_classic_button(Btn &w) {
  lv_obj_set_style_bg_opa(w.root, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_opa(w.root, LV_OPA_COVER, LV_STATE_PRESSED);
  lv_obj_add_flag(w.plate, LV_OBJ_FLAG_HIDDEN);
  lv_obj_remove_flag(w.lbl, LV_OBJ_FLAG_HIDDEN);
  ui_set_text_color(w.lbl, COL_TEXT);
}

static void render_buttons() {
  const DeckPage &p = s_layout.page[s_page];
  const int cols = p.cols >= 6 ? 6 : 4;
  const int max = cols * GRID_ROWS;
  const bool icons = s_layout.style == DECK_ICONS;
  const bool labels = !icons || s_layout.labels;
  for (int i = 0; i < MAX_BTNS; i++) {
    Btn &w = s_btns[i];
    if (i >= p.n_btn || i >= max) {
      lv_obj_add_flag(w.root, LV_OBJ_FLAG_HIDDEN);
      continue;
    }
    const DeckButton &b = p.btn[i];
    w.color = b.color ? b.color : page_color(s_page);
    lv_obj_remove_flag(w.root, LV_OBJ_FLAG_HIDDEN);
    layout_button(w, i, cols);

    const lv_image_dsc_t *app = icon_store_get(b.img);
    if (app) {
      lv_image_set_src(w.img, app);
      lv_obj_remove_flag(w.img, LV_OBJ_FLAG_HIDDEN);
      lv_obj_add_flag(w.ico, LV_OBJ_FLAG_HIDDEN);
      lv_obj_align(w.img, LV_ALIGN_CENTER, 0, -12);
    } else {
      lv_obj_add_flag(w.img, LV_OBJ_FLAG_HIDDEN);
      lv_obj_remove_flag(w.ico, LV_OBJ_FLAG_HIDDEN);
      if (strcmp(b.icon, "spark") == 0) {
        lv_obj_set_style_text_font(w.ico, &font_mono_32, 0);
        ui_set_text(w.ico, "\xE2\x9C\xBB"); /* Claude's asterisk */
      } else {
        lv_obj_set_style_text_font(w.ico, &font_icons_36, 0);
        const char *g = icon(b.icon);
        ui_set_text(w.ico, g[0] ? g : icon(b.img[0] ? "apps" : "bolt"));
      }
      ui_set_text_color(w.ico, w.color);
      lv_obj_align(w.ico, LV_ALIGN_CENTER, 0, -14);
    }
    ui_set_text(w.lbl, b.label);
    lv_obj_set_style_bg_color(w.root, C(ui_mix(COL_SURFACE, w.color, 70)), LV_STATE_PRESSED);
    if (icons)
      style_icons_button(w, app != nullptr, labels);
    else
      style_classic_button(w);
    apply_mic_state(i, b);
    apply_media_state(i, b);
  }
}

static void build_tabs() {
  lv_obj_clean(s_tabs_row);
  for (int i = 0; i < s_layout.n_page; i++) {
    lv_obj_t *t = ui_card(s_tabs_row, COL_SURFACE, 20);
    lv_obj_set_height(t, 40);
    lv_obj_set_width(t, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(t, 20, 0);
    lv_obj_add_flag(t, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_t *l = ui_label(t, &lv_font_montserrat_16, COL_TEXT_DIM, s_layout.page[i].name);
    lv_obj_set_style_text_letter_space(l, 2, 0);
    lv_obj_center(l);
    lv_obj_add_event_cb(t, on_tab, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    s_tabs[i] = t;
    style_tab(i, i == s_page);
  }
}

void deck_page_build(lv_obj_t *tile) {
  s_tabs_row = ui_box(tile);
  lv_obj_set_pos(s_tabs_row, 16, 10);
  lv_obj_set_size(s_tabs_row, 768, 40);
  lv_obj_set_flex_flow(s_tabs_row, LV_FLEX_FLOW_ROW);
  lv_obj_set_style_pad_column(s_tabs_row, 10, 0);

  s_usb_note = ui_label(tile, &lv_font_montserrat_14, COL_AMBER, "");
  lv_obj_align(s_usb_note, LV_ALIGN_TOP_RIGHT, -18, 22);

  s_grid = ui_box(tile);
  lv_obj_set_pos(s_grid, 16, 62);
  lv_obj_set_size(s_grid, GRID_W, GRID_ROWS * BTN_H + (GRID_ROWS - 1) * GAP);
  for (int i = 0; i < MAX_BTNS; i++) {
    Btn &w = s_btns[i];
    w.root = ui_card(s_grid, COL_SURFACE, 16);
    lv_obj_add_flag(w.root, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_translate_y(w.root, 2, LV_STATE_PRESSED);
    w.plate = ui_card(w.root, COL_CLAY, 18); /* behind the glyph: created first */
    lv_obj_remove_flag(w.plate, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(w.plate, LV_OBJ_FLAG_HIDDEN);
    w.ico = ui_label(w.root, &font_icons_36, COL_CLAY, "");
    w.img = lv_image_create(w.root);
    lv_obj_add_flag(w.img, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(w.img, LV_OBJ_FLAG_CLICKABLE);
    w.lbl = ui_label(w.root, &lv_font_montserrat_18, COL_TEXT, "");
    lv_obj_set_style_text_align(w.lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_add_event_cb(w.root, on_btn, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    layout_button(w, i, 4);
    lv_obj_add_flag(w.root, LV_OBJ_FLAG_HIDDEN);
  }
  s_built = true;
}

void deck_page_set_layout(const DeckLayout &d) {
  s_layout = d;
  if (s_page >= s_layout.n_page) s_page = 0;
  if (!s_built || s_layout.n_page == 0) return;
  build_tabs();
  render_buttons();
}

void deck_page_update(const Model &m) {
  bool muted = m.pc.ok && m.pc.mic_muted;
  if (muted != s_mic_muted) {
    s_mic_muted = muted;
    const DeckPage &p = s_layout.page[s_page];
    for (int i = 0; i < p.n_btn && i < MAX_BTNS; i++) apply_mic_state(i, p.btn[i]);
  }
  bool playing = music_playing();
  if (playing != s_playing) {
    s_playing = playing;
    const DeckPage &p = s_layout.page[s_page];
    for (int i = 0; i < p.n_btn && i < MAX_BTNS; i++) apply_media_state(i, p.btn[i]);
  }
  if (icon_store_rev() != s_icons_rev && s_layout.n_page) {
    s_icons_rev = icon_store_rev();
    render_buttons();
  }
  static int s_usb_shown = -1;
  int usb = hid_ready() ? 1 : 0;
  if (usb != s_usb_shown) {
    s_usb_shown = usb;
    ui_set_text(s_usb_note, usb ? "" : "USB keys offline");
  }
}
