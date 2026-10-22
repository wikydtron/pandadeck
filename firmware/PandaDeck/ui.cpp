/* UI shell: top bar (page tabs + status cluster + clock), a tileview with
 * the pages the settings list (AI / DECK / MUSIC / HUD, in the user's
 * order), overlays, and the refresh loop that pulls snapshots from the
 * store. A page is built the first time it is listed; a changed list
 * re-orders tiles and tabs live, no reboot. */
#include "ui.h"

#include "display.h"
#include "hid.h"
#include "net.h"
#include "settings.h"
#include "store.h"
#include "ui_pages.h"
#include "ui_theme.h"

#include <Arduino.h>
#include <stdlib.h>
#include <time.h>

#define AGENT_OK_MS 4000

struct PageSlot {
  lv_obj_t *root; /* full-page container handed to the page's build() */
  bool built;
};

static PageSlot s_slot[PAGE_ID_COUNT];
static PageList s_shown;                  /* tabs, in order */
static lv_obj_t *s_tiles[PAGE_ID_COUNT];  /* by tab index */
static lv_obj_t *s_tab_hit[PAGE_ID_COUNT];
static lv_obj_t *s_tv, *s_holder, *s_tabbar, *s_underline;
static lv_obj_t *s_clock, *s_wifi, *s_pc_dot, *s_pc_lbl, *s_usb;
static int s_page = 0; /* tab index */

static Model *s_model; /* UI-side copy (PSRAM) */
static DeckLayout *s_deck;
static uint32_t s_seen_version = 0;
static uint32_t s_seen_deck = 0;
static uint32_t s_seen_pages = 0, s_seen_settings = 0;
static uint8_t s_brightness = 100;

static bool agent_ok() { return store_ms_since_sync() < AGENT_OK_MS; }

/* ---- pages ------------------------------------------------------------------ */
static bool built(uint8_t id) { return id < PAGE_ID_COUNT && s_slot[id].built; }

static void page_update(uint8_t id, const Model &m, bool ok) {
  switch (id) {
    case PAGE_ID_AI: claude_page_update(m, ok); break;
    case PAGE_ID_MUSIC: music_page_update(m, ok); break;
    case PAGE_ID_HUD: hud_page_update(m, ok); break;
    default: break; /* DECK refreshes every tick */
  }
}

static void build_page(uint8_t id) {
  if (id >= PAGE_ID_COUNT || s_slot[id].built) return;
  lv_obj_t *root = ui_box(s_holder);
  lv_obj_set_size(root, 800, CONTENT_H);
  lv_obj_remove_flag(root, LV_OBJ_FLAG_CLICKABLE); /* swipes reach the tile */
  switch (id) {
    case PAGE_ID_AI: claude_page_build(root); break;
    case PAGE_ID_DECK: deck_page_build(root); break;
    case PAGE_ID_MUSIC: music_page_build(root); break;
    case PAGE_ID_HUD: hud_page_build(root); break;
    default: break;
  }
  s_slot[id].root = root;
  s_slot[id].built = true;
  if (id == PAGE_ID_DECK) deck_page_set_layout(*s_deck);
  page_update(id, *s_model, agent_ok());
}

/* ---- tabs ------------------------------------------------------------------- */
static void underline_x_cb(void *obj, int32_t v) { lv_obj_set_x((lv_obj_t *)obj, v); }
static void underline_w_cb(void *obj, int32_t v) { lv_obj_set_width((lv_obj_t *)obj, v); }

static void move_underline(int page, bool anim) {
  lv_obj_update_layout(s_tab_hit[page]);
  int32_t x = lv_obj_get_x(s_tab_hit[page]);
  int32_t w = lv_obj_get_width(s_tab_hit[page]);
  if (!anim) {
    lv_obj_set_x(s_underline, x);
    lv_obj_set_width(s_underline, w);
    return;
  }
  lv_anim_t a;
  lv_anim_init(&a);
  lv_anim_set_var(&a, s_underline);
  lv_anim_set_duration(&a, 220);
  lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
  lv_anim_set_values(&a, lv_obj_get_x(s_underline), x);
  lv_anim_set_exec_cb(&a, underline_x_cb);
  lv_anim_start(&a);
  lv_anim_set_values(&a, lv_obj_get_width(s_underline), w);
  lv_anim_set_exec_cb(&a, underline_w_cb);
  lv_anim_start(&a);
}

static void set_page(int page, bool from_swipe, bool anim = true) {
  if (page < 0 || page >= s_shown.n) return;
  s_page = page;
  for (int i = 0; i < s_shown.n; i++)
    lv_obj_set_style_text_color(s_tab_hit[i], C(i == page ? COL_TEXT : COL_TEXT_FAINT), 0);
  move_underline(page, anim);
  /* Instant switch on tab tap: a full-screen slide costs several heavy frames
   * on this panel and made taps feel late. Swipes still scroll naturally. */
  if (!from_swipe) lv_tileview_set_tile_by_index(s_tv, page, 0, LV_ANIM_OFF);
}

static void on_tab(lv_event_t *e) { set_page((int)(intptr_t)lv_event_get_user_data(e), false); }

static void on_tile_changed(lv_event_t *) {
  lv_obj_t *t = lv_tileview_get_tile_active(s_tv);
  for (int i = 0; i < s_shown.n; i++)
    if (s_tiles[i] == t && i != s_page) set_page(i, true);
}

/* Tab labels + underline for the current list (same look as ever). */
static void build_tabs() {
  lv_obj_clean(s_tabbar);
  int x = 6;  /* tabs start at the left edge (no logo) */
  for (int i = 0; i < s_shown.n; i++) {
    lv_obj_t *hit = ui_box(s_tabbar);
    lv_obj_set_size(hit, LV_SIZE_CONTENT, TOPBAR_H);
    lv_obj_set_pos(hit, x, 0);
    lv_obj_set_style_pad_hor(hit, 12, 0);
    lv_obj_add_flag(hit, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_t *l = ui_label(hit, &lv_font_montserrat_16, COL_TEXT_FAINT, page_id_label(s_shown.ids[i]));
    lv_obj_set_style_text_letter_space(l, 3, 0);
    lv_obj_align(l, LV_ALIGN_LEFT_MID, 0, 0);
    lv_obj_add_event_cb(hit, on_tab, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    s_tab_hit[i] = hit;
    lv_obj_update_layout(hit);
    x += lv_obj_get_width(hit) + 6;
  }
  s_underline = ui_card(s_tabbar, COL_CLAY, 2);
  lv_obj_set_size(s_underline, 60, 3);
  lv_obj_set_y(s_underline, TOPBAR_H - 6);
}

/* (Re)creates tiles + tabs for the settings' page list. Pages already built
 * keep their objects and state; they're only moved to their new tile. */
static void apply_pages(const PageList &want) {
  uint8_t keep = s_shown.n > 0 ? s_shown.ids[s_page] : 0xFF;
  for (int i = 0; i < PAGE_ID_COUNT; i++)
    if (s_slot[i].built) lv_obj_set_parent(s_slot[i].root, s_holder);
  if (s_tv) lv_obj_delete(s_tv);

  s_shown = want;
  s_tv = lv_tileview_create(lv_screen_active());
  lv_obj_move_to_index(s_tv, 0); /* under the top bar and the holder */
  lv_obj_set_size(s_tv, 800, CONTENT_H);
  lv_obj_set_pos(s_tv, 0, TOPBAR_H);
  lv_obj_set_style_bg_opa(s_tv, LV_OPA_TRANSP, 0);
  lv_obj_set_scrollbar_mode(s_tv, LV_SCROLLBAR_MODE_OFF);
  int page = 0;
  for (int i = 0; i < s_shown.n; i++) {
    uint8_t id = s_shown.ids[i];
    s_tiles[i] = lv_tileview_add_tile(s_tv, i, 0, (lv_dir_t)(LV_DIR_LEFT | LV_DIR_RIGHT));
    lv_obj_set_style_bg_opa(s_tiles[i], LV_OPA_TRANSP, 0);
    lv_obj_set_scrollbar_mode(s_tiles[i], LV_SCROLLBAR_MODE_OFF);
    build_page(id);
    lv_obj_set_parent(s_slot[id].root, s_tiles[i]);
    lv_obj_set_pos(s_slot[id].root, 0, 0);
    if (id == keep) page = i;
  }
  lv_obj_add_event_cb(s_tv, on_tile_changed, LV_EVENT_VALUE_CHANGED, nullptr);
  build_tabs();
  set_page(page, false, false);
}

/* ---- top bar ----------------------------------------------------------------- */
#define FLIP_HOLD_MS 2000

static void on_mark(lv_event_t *e) {
  static uint32_t s_down = 0;
  static bool s_done = false;
  lv_event_code_t code = lv_event_get_code(e);
  if (code == LV_EVENT_PRESSED) {
    s_down = millis();
    s_done = false;
  } else if (code == LV_EVENT_PRESSING && s_down && !s_done && millis() - s_down >= FLIP_HOLD_MS) {
    s_done = true;
    display_request_flip(!display_flip_wanted());
  } else if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
    s_down = 0;
  }
}

static lv_obj_t *s_gear;

static void gear_reset_cb(lv_timer_t *tm) {
  ui_set_text_color(s_gear, COL_TEXT_DIM);
  lv_timer_delete(tm);
}

static void on_gear(lv_event_t *) {
  bool ok = store_ms_since_sync() < AGENT_OK_MS;
  if (ok) store_push_event("cmd", "", "settings");
  ui_set_text_color(s_gear, ok ? COL_GREEN : COL_RED); /* sent / no PC app to ask */
  lv_timer_create(gear_reset_cb, 700, nullptr);
}

static void build_topbar(lv_obj_t *scr) {
  lv_obj_t *bar = ui_box(scr);
  lv_obj_set_size(bar, 800, TOPBAR_H);

  /* tabs live in their own non-clickable layer so they can be rebuilt */
  s_tabbar = ui_box(bar);
  lv_obj_set_size(s_tabbar, 800, TOPBAR_H);
  lv_obj_remove_flag(s_tabbar, LV_OBJ_FLAG_CLICKABLE);

  /* status cluster, right-aligned: [PC dot] PC   [usb]   [wifi]   12:34 */
  s_clock = ui_label(bar, &font_mono_20, COL_TEXT, "--:--");
  lv_obj_align(s_clock, LV_ALIGN_RIGHT_MID, -18, 0);
  /* Hold the clock 2 s to flip the whole screen 180 deg (when the panel sits the
   * other way round) -- works without the PC app. */
  lv_obj_t *clock_hit = ui_box(bar);
  lv_obj_set_size(clock_hit, 84, TOPBAR_H);
  lv_obj_align(clock_hit, LV_ALIGN_RIGHT_MID, -4, 0);
  lv_obj_add_flag(clock_hit, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_add_event_cb(clock_hit, on_mark, LV_EVENT_ALL, nullptr);
  s_wifi = ui_label(bar, &font_icons_20, COL_TEXT_FAINT, icon("wifi"));
  lv_obj_align(s_wifi, LV_ALIGN_RIGHT_MID, -96, 0);
  s_usb = ui_label(bar, &font_icons_20, COL_TEXT_FAINT, icon("keyboard"));
  lv_obj_align(s_usb, LV_ALIGN_RIGHT_MID, -132, 0);
  s_pc_lbl = ui_label(bar, &lv_font_montserrat_14, COL_TEXT_DIM, "PC");
  lv_obj_set_style_text_letter_space(s_pc_lbl, 2, 0);
  lv_obj_align(s_pc_lbl, LV_ALIGN_RIGHT_MID, -172, 0);
  s_pc_dot = ui_card(bar, COL_TEXT_FAINT, 5);
  lv_obj_set_size(s_pc_dot, 10, 10);
  lv_obj_align(s_pc_dot, LV_ALIGN_RIGHT_MID, -202, 0);

  /* Settings: asks the PC app to open its settings page (it knows its own
   * address/port, so nothing to configure here). */
  lv_obj_t *gear = ui_box(bar);
  lv_obj_set_size(gear, 44, TOPBAR_H);
  lv_obj_align(gear, LV_ALIGN_RIGHT_MID, -222, 0);
  lv_obj_add_flag(gear, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_style_bg_color(gear, C(COL_SURFACE2), LV_STATE_PRESSED);
  lv_obj_set_style_bg_opa(gear, LV_OPA_COVER, LV_STATE_PRESSED);
  lv_obj_set_style_radius(gear, 10, LV_STATE_PRESSED);
  lv_obj_add_event_cb(gear, on_gear, LV_EVENT_CLICKED, nullptr);
  s_gear = ui_label(gear, &font_icons_20, COL_TEXT_DIM, icon("settings"));
  lv_obj_center(s_gear);

  lv_obj_t *rule = ui_card(scr, COL_LINE, 0);
  lv_obj_set_size(rule, 800, 1);
  lv_obj_set_pos(rule, 0, TOPBAR_H - 1);
}

/* Every style/text setter invalidates (= redraw + PSRAM traffic) even when the
 * value is unchanged, so the status cluster only touches what changed. */
static void update_status() {
  static uint32_t s_dot = 0xFFFFFFFF, s_wifi_col = 0xFFFFFFFF;
  static int s_usb_state = -1;
  static char s_clock_txt[8] = "";

  uint32_t since = store_ms_since_sync();
  uint32_t dot = since < AGENT_OK_MS ? COL_GREEN : since < 20000 ? COL_AMBER : COL_RED;
  if (since == UINT32_MAX) dot = COL_TEXT_FAINT;
  if (dot != s_dot) {
    s_dot = dot;
    lv_obj_set_style_bg_color(s_pc_dot, C(dot), 0);
  }
  uint32_t wcol = !net_wifi_configured() ? COL_TEXT_FAINT
                  : !net_connected()     ? COL_RED
                  : net_rssi() > -70     ? COL_TEXT_DIM
                                         : COL_AMBER;
  if (wcol != s_wifi_col) {
    s_wifi_col = wcol;
    lv_obj_set_style_text_color(s_wifi, C(wcol), 0);
  }
  int usb = hid_ready() ? 1 : 0;
  if (usb != s_usb_state) {
    s_usb_state = usb;
    lv_obj_set_style_text_color(s_usb, C(usb ? COL_TEXT_DIM : COL_TEXT_FAINT), 0);
    lv_obj_set_style_opa(s_usb, usb ? LV_OPA_COVER : LV_OPA_40, 0);
  }

  if (net_time_ok()) {
    time_t now = time(nullptr);
    struct tm ti;
    localtime_r(&now, &ti);
    char b[8];
    snprintf(b, sizeof b, "%d:%02d", ti.tm_hour, ti.tm_min);
    if (strcmp(b, s_clock_txt) != 0) {
      strcpy(s_clock_txt, b);
      lv_label_set_text(s_clock, b);
    }
  }
}

/* ---- settings ------------------------------------------------------------------ */
/* Brightness is re-asserted every tick so a debug override can't stick; no
 * idle dimming. Time zone changes apply here, on the thread that formats
 * the clock. */
static void apply_settings() {
  if (settings_rev() != s_seen_settings) {
    s_seen_settings = settings_rev();
    Settings s = settings_get();
    s_brightness = s.brightness;
    const char *tz = getenv("TZ");
    if (!tz || strcmp(tz, s.tz) != 0) {
      setenv("TZ", s.tz, 1);
      tzset();
    }
    if (settings_pages_rev() != s_seen_pages) {
      s_seen_pages = settings_pages_rev();
      apply_pages(s.pages);
    }
  }
  display_set_brightness(s_brightness);
}

/* ---- refresh ------------------------------------------------------------------------ */
static void refresh_timer(lv_timer_t *) {
  display_apply_pending_flip();
  apply_settings();
  bool ok = agent_ok();
  if (store_deck(s_deck, &s_seen_deck) && built(PAGE_ID_DECK)) deck_page_set_layout(*s_deck);
  static bool s_last_ok = false;
  bool changed = store_snapshot(s_model, &s_seen_version);
  if (!ok) {
    /* Stale snapshot from a dead agent: never show old permission prompts
     * or frozen sessions as if they were live. */
    s_model->n_sessions = 0;
    s_model->n_perms = 0;
    s_model->usage.ok = s_model->pc.ok = s_model->lab.ok = s_model->media.ok = false;
  }
  if (changed || ok != s_last_ok) {
    for (int id = 0; id < PAGE_ID_COUNT; id++)
      if (built(id)) page_update(id, *s_model, ok);
    if (!built(PAGE_ID_MUSIC)) music_page_update(*s_model, ok); /* deck mirrors play state */
    perm_overlay_update(*s_model);
    s_last_ok = ok;
  }
  music_page_tick();
  if (built(PAGE_ID_DECK)) deck_page_update(*s_model);
  update_status();
}

static void sample_timer(lv_timer_t *) {
  if (built(PAGE_ID_HUD)) hud_page_sample(*s_model);
}

void ui_init() {
  s_model = (Model *)ps_calloc(1, sizeof(Model));
  s_deck = (DeckLayout *)ps_calloc(1, sizeof(DeckLayout));
  store_deck(s_deck, &s_seen_deck);

  lv_obj_t *scr = lv_screen_active();
  lv_obj_set_style_bg_color(scr, C(COL_BG), 0);
  lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
  lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

  /* parking spot for built pages that aren't listed (or between rebuilds) */
  s_holder = ui_box(scr);
  lv_obj_add_flag(s_holder, LV_OBJ_FLAG_HIDDEN);

  build_topbar(scr);
  perm_overlay_build();

  Settings s = settings_get();
  s_brightness = s.brightness;
  s_seen_settings = settings_rev();
  s_seen_pages = settings_pages_rev();
  apply_pages(s.pages);
  music_page_update(*s_model, false);

  lv_timer_create(refresh_timer, 250, nullptr);
  lv_timer_create(sample_timer, 2000, nullptr);
}
