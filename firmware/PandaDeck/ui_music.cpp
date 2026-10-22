/* MUSIC page — now playing on your computer (Spotify or any player the
 * desktop app can see), pushed by the app's media collector.
 *
 *   ~~~~~~~~ blurred, darkened cover as the page backdrop ~~~~~~~~
 *   [ cover ]   SPOTIFY
 *   [ 248px ]   Title (big, or 2 lines)
 *               Artist / Album
 *   1:02  ======================o-----------------------  3:53
 *   [shuffle]  [  |<  ]  [     (>)     ]  [  >|  ]  [repeat]
 *
 * Big pads sized for a finger. Play/pause/next/prev go out as USB media keys
 * (instant), falling back to the agent when USB is down; seek (drag or tap
 * the bar), shuffle and repeat go through the agent. Play/pause is
 * optimistic: it flips on tap and the snapshot confirms it a moment later.
 * The position ticks locally between syncs. */
#include "backdrop.h"
#include "deck_action.h"
#include "hid.h"
#include "icon_store.h"
#include "store.h"
#include "ui_pages.h"
#include "ui_theme.h"

#include <Arduino.h>
#include <stdio.h>
#include <string.h>

#define ART_PX 248
#define ART_X 40
#define ART_Y 22
#define COL_X (ART_X + ART_PX + 28)
#define COL_W (760 - COL_X)
#define BAR_CY 300      /* progress row centre */
#define CTRL_CY 376     /* transport row centre */
#define VOL_CY (ART_Y + ART_PX - 14)  /* volume row centre, level with the cover's bottom */
#define VOL_SEND_MS 150 /* while dragging, at most this often */
#define OPTIMISTIC_MS 2500
#define RESYNC_DRIFT_S 2
#define AGENT_OK_MS 4000
#define GLASS_OPA LV_OPA_40

static lv_obj_t *s_root, *s_wash, *s_bg;
static lv_obj_t *s_art, *s_art_ph, *s_art_ph_ico;
static lv_obj_t *s_dot, *s_app, *s_title, *s_artist, *s_album;
static lv_obj_t *s_slider, *s_pos, *s_dur;
static lv_obj_t *s_btn_shuffle, *s_btn_prev, *s_btn_play, *s_btn_next, *s_btn_repeat;
static lv_obj_t *s_play_ico;
static lv_obj_t *s_btn_heart, *s_heart_ico;
static lv_obj_t *s_vol_row, *s_vol_ico, *s_vol_slider, *s_vol_pct;
static bool s_vol_drag = false;
static uint32_t s_vol_hold_until = 0, s_vol_sent_at = 0;
static int8_t s_pc_vol = -1; /* the computer's output volume from the snapshot */
/* optimistic like: flips on tap until the snapshot agrees (or 4 s pass) */
static int8_t s_opt_liked = -2;
static uint32_t s_liked_until = 0;
static void render_heart();
static lv_obj_t *s_empty, *s_empty_sub;

static MediaInfo s_m;           /* last snapshot (agent alive) */
static bool s_ok = false;       /* snapshot has a live media session */
static bool s_built = false;    /* page listed in the settings */
static uint32_t s_tint = 0;     /* 0 = not styled yet */
static char s_shown_art[ICON_TAG_LEN] = "\x01"; /* force first render */
static char s_shown_bg[BACKDROP_TAG_LEN] = "\x01";
static uint32_t s_shown_icons_rev = 0;

/* local clock for the progress bar */
static int32_t s_base_pos = 0;
static uint32_t s_base_ms = 0;
static int32_t s_shown_sec = -1;
static bool s_scrubbing = false; /* finger on the slider: don't fight it */

/* optimistic play state */
static bool s_opt = false, s_opt_playing = false;
static uint32_t s_opt_until = 0;
static int s_shown_playing = -1;

static bool model_playing() { return s_ok && s_m.playing; }

bool music_playing() {
  if (s_opt && (int32_t)(s_opt_until - millis()) > 0) return s_opt_playing;
  s_opt = false;
  return model_playing();
}

void music_toggle_optimistic() {
  s_opt_playing = !music_playing();
  s_opt = true;
  s_opt_until = millis() + OPTIMISTIC_MS;
}

static int32_t live_pos() {
  int32_t p = s_base_pos;
  if (music_playing()) p += (int32_t)((millis() - s_base_ms) / 1000);
  if (s_m.dur_s > 0 && p > s_m.dur_s) p = s_m.dur_s;
  return p < 0 ? 0 : p;
}

static void rebase(int32_t pos) {
  s_base_pos = pos;
  s_base_ms = millis();
  s_shown_sec = -1;
}

static void fmt_time(char *b, size_t n, int32_t s) {
  if (s >= 3600)
    snprintf(b, n, "%ld:%02ld:%02ld", (long)(s / 3600), (long)((s / 60) % 60), (long)(s % 60));
  else
    snprintf(b, n, "%ld:%02ld", (long)(s / 60), (long)(s % 60));
}

/* ---- actions ------------------------------------------------------------------ */
static bool agent_alive() { return store_ms_since_sync() < AGENT_OK_MS; }

/* Transport: USB media key if the PC enumerated us, else the agent. */
static void transport(const char *media_key, const char *agent_cmd) {
  if (hid_ready()) {
    DeckAction a;
    if (deck_parse_action(media_key, &a)) hid_fire(a);
  } else if (agent_alive()) {
    store_push_event("cmd", "", agent_cmd);
  }
}

static void on_play(lv_event_t *) {
  if (!s_ok) return;
  int32_t here = live_pos(); /* freeze / resume from where the bar visually is */
  music_toggle_optimistic();
  rebase(here);
  transport("media:playpause", "media:playpause");
}

static void on_prev(lv_event_t *) {
  if (s_ok) transport("media:prev", "media:prev");
}
static void on_next(lv_event_t *) {
  if (s_ok) transport("media:next", "media:next");
}

static void on_shuffle(lv_event_t *) {
  if (s_ok && agent_alive()) store_push_event("cmd", "", "media:shuffle");
}
static int8_t liked_now() {
  if (s_opt_liked != -2 && (int32_t)(s_liked_until - millis()) > 0) return s_opt_liked;
  s_opt_liked = -2;
  return s_ok ? s_m.liked : -1;
}

/* ❤: save/remove the song in Spotify's Liked Songs. Only shown while Spotify is
 * connected and playing (liked 0/1); -1 hides it (another player, no sign-in). */
static void on_heart(lv_event_t *) {
  if (!s_ok || store_ms_since_sync() > AGENT_OK_MS) return;
  int8_t cur = liked_now();
  if (cur < 0) return;
  s_opt_liked = cur ? 0 : 1;
  s_liked_until = millis() + 4000;
  store_push_event("cmd", "", "media:like");
  render_heart();
}

/* Volume: the computer's output volume. Sent live while dragging (throttled)
 * and on release; the snapshot's value is ignored for a moment after. */
static void render_vol(int32_t v) {
  char b[8];
  snprintf(b, sizeof b, "%ld%%", (long)v);
  ui_set_text(s_vol_pct, b);
  ui_set_text(s_vol_ico, icon(v == 0 ? "mute" : "vol_up"));
}

static void send_volume(int32_t v) {
  char cmd[20];
  snprintf(cmd, sizeof cmd, "volume:%ld", (long)v);
  store_push_event("cmd", "", cmd);
  s_vol_sent_at = millis();
}

static void on_volume(lv_event_t *e) {
  lv_event_code_t code = lv_event_get_code(e);
  int32_t v = lv_slider_get_value(s_vol_slider);
  if (code == LV_EVENT_PRESSED) {
    s_vol_drag = true;
  } else if (code == LV_EVENT_VALUE_CHANGED) {
    render_vol(v);
    if (s_vol_drag && agent_alive() && millis() - s_vol_sent_at >= VOL_SEND_MS) send_volume(v);
  } else if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
    if (!s_vol_drag) return;
    s_vol_drag = false;
    if (agent_alive()) send_volume(v);
    s_vol_hold_until = millis() + OPTIMISTIC_MS;
  }
}

/* The speaker icon: mute / unmute, as the USB keyboard's mute key. */
static void on_vol_mute(lv_event_t *) {
  DeckAction a;
  if (deck_parse_action("media:mute", &a)) hid_fire(a);
}

static void on_repeat(lv_event_t *) {
  if (s_ok && agent_alive()) store_push_event("cmd", "", "media:repeat");
}

/* Drag (or tap) the bar: preview the time while the finger is down, seek on
 * release. */
static void on_slider(lv_event_t *e) {
  lv_event_code_t code = lv_event_get_code(e);
  if (!s_ok || s_m.dur_s <= 0) return;
  if (code == LV_EVENT_PRESSED) {
    s_scrubbing = true;
  } else if (code == LV_EVENT_VALUE_CHANGED && s_scrubbing) {
    char b[16];
    fmt_time(b, sizeof b, lv_slider_get_value(s_slider));
    ui_set_text(s_pos, b);
  } else if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
    if (!s_scrubbing) return;
    s_scrubbing = false;
    int32_t target = lv_slider_get_value(s_slider);
    if (!agent_alive()) {
      s_shown_sec = -1; /* snap back */
      return;
    }
    char cmd[32];
    snprintf(cmd, sizeof cmd, "media:seek:%ld", (long)target);
    store_push_event("cmd", "", cmd);
    rebase(target); /* jump now; the snapshot confirms within a sync */
  }
}

/* ---- widgets ---------------------------------------------------------------------- */
/* A touch pad: translucent "glass" over the backdrop, finger-sized. */
static lv_obj_t *pad(lv_obj_t *parent, int w, int h, int x, lv_event_cb_t cb) {
  lv_obj_t *b = ui_card(parent, 0x000000, 20);
  lv_obj_set_size(b, w, h);
  lv_obj_set_pos(b, x, CTRL_CY - h / 2);
  lv_obj_set_style_bg_opa(b, GLASS_OPA, 0);
  lv_obj_set_style_border_width(b, 1, 0);
  lv_obj_set_style_border_color(b, C(0xFFFFFF), 0);
  lv_obj_set_style_border_opa(b, LV_OPA_10, 0);
  lv_obj_set_style_bg_opa(b, LV_OPA_70, LV_STATE_PRESSED);
  lv_obj_set_style_translate_y(b, 2, LV_STATE_PRESSED);
  lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_ext_click_area(b, 6);
  lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, nullptr);
  return b;
}

/* Pad with a glyph only. */
static lv_obj_t *icon_pad(lv_obj_t *parent, int w, int h, int x, const lv_font_t *font, const char *glyph,
                          lv_event_cb_t cb, lv_obj_t **ico_out) {
  lv_obj_t *b = pad(parent, w, h, x, cb);
  lv_obj_t *l = ui_label(b, font, COL_TEXT, glyph);
  lv_obj_center(l);
  if (ico_out) *ico_out = l;
  return b;
}

/* Toggle pad: small glyph over a caption (SHUFFLE / REPEAT). */
static lv_obj_t *toggle_pad(lv_obj_t *parent, int w, int h, int x, const char *glyph, const char *caption,
                            lv_event_cb_t cb) {
  lv_obj_t *b = pad(parent, w, h, x, cb);
  lv_obj_t *ico = ui_label(b, &font_icons_20, COL_TEXT_DIM, glyph);
  lv_obj_align(ico, LV_ALIGN_CENTER, 0, -10);
  lv_obj_t *cap = ui_label(b, &lv_font_montserrat_12, COL_TEXT_FAINT, caption);
  lv_obj_set_style_text_letter_space(cap, 2, 0);
  lv_obj_align(cap, LV_ALIGN_CENTER, 0, 16);
  return b;
}

static void build_empty(lv_obj_t *tile) {
  s_empty = ui_box(tile);
  lv_obj_set_size(s_empty, 800, CONTENT_H);
  lv_obj_t *ring = ui_card(s_empty, COL_SURFACE, LV_RADIUS_CIRCLE);
  lv_obj_set_size(ring, 120, 120);
  lv_obj_align(ring, LV_ALIGN_CENTER, 0, -70);
  lv_obj_t *ico = ui_label(ring, &font_icons_36, COL_CLAY, icon("music"));
  lv_obj_center(ico);
  lv_obj_t *h = ui_label(s_empty, &font_head_28, COL_TEXT, "Nothing playing");
  lv_obj_align(h, LV_ALIGN_CENTER, 0, 24);
  s_empty_sub = ui_label(s_empty, &lv_font_montserrat_16, COL_TEXT_DIM, "");
  lv_obj_align(s_empty_sub, LV_ALIGN_CENTER, 0, 62);
}

static void build_slider() {
  const int x = 112, w = 576;
  s_pos = ui_label(s_root, &font_mono_16, COL_TEXT, "0:00");
  lv_obj_set_width(s_pos, 64);
  lv_obj_set_style_text_align(s_pos, LV_TEXT_ALIGN_RIGHT, 0);
  lv_obj_set_pos(s_pos, x - 16 - 64, BAR_CY - 10);
  s_dur = ui_label(s_root, &font_mono_16, COL_TEXT_DIM, "");
  lv_obj_set_pos(s_dur, x + w + 16, BAR_CY - 10);

  s_slider = lv_slider_create(s_root);
  lv_obj_remove_style_all(s_slider);
  lv_obj_set_size(s_slider, w, 8);
  lv_obj_set_pos(s_slider, x, BAR_CY - 4);
  lv_slider_set_range(s_slider, 0, 1);
  lv_obj_set_style_bg_color(s_slider, C(0xFFFFFF), LV_PART_MAIN);
  lv_obj_set_style_bg_opa(s_slider, LV_OPA_20, LV_PART_MAIN);
  lv_obj_set_style_radius(s_slider, 4, LV_PART_MAIN);
  lv_obj_set_style_bg_color(s_slider, C(COL_CLAY), LV_PART_INDICATOR);
  lv_obj_set_style_bg_opa(s_slider, LV_OPA_COVER, LV_PART_INDICATOR);
  lv_obj_set_style_radius(s_slider, 4, LV_PART_INDICATOR);
  lv_obj_set_style_bg_color(s_slider, C(COL_TEXT), LV_PART_KNOB);
  lv_obj_set_style_bg_opa(s_slider, LV_OPA_COVER, LV_PART_KNOB);
  lv_obj_set_style_radius(s_slider, LV_RADIUS_CIRCLE, LV_PART_KNOB);
  lv_obj_set_style_pad_all(s_slider, 7, LV_PART_KNOB);  /* 22 px knob */
  lv_obj_set_style_pad_all(s_slider, 12, LV_STATE_PRESSED | LV_PART_KNOB);
  lv_obj_set_ext_click_area(s_slider, 22);
  lv_obj_add_event_cb(s_slider, on_slider, LV_EVENT_ALL, nullptr);
}

static void style_bar(lv_obj_t *sl, uint32_t indicator) {
  lv_obj_remove_style_all(sl);
  lv_obj_set_style_bg_color(sl, C(0xFFFFFF), LV_PART_MAIN);
  lv_obj_set_style_bg_opa(sl, LV_OPA_20, LV_PART_MAIN);
  lv_obj_set_style_radius(sl, 4, LV_PART_MAIN);
  lv_obj_set_style_bg_color(sl, C(indicator), LV_PART_INDICATOR);
  lv_obj_set_style_bg_opa(sl, LV_OPA_COVER, LV_PART_INDICATOR);
  lv_obj_set_style_radius(sl, 4, LV_PART_INDICATOR);
  lv_obj_set_style_bg_color(sl, C(COL_TEXT), LV_PART_KNOB);
  lv_obj_set_style_bg_opa(sl, LV_OPA_COVER, LV_PART_KNOB);
  lv_obj_set_style_radius(sl, LV_RADIUS_CIRCLE, LV_PART_KNOB);
  lv_obj_set_style_pad_all(sl, 7, LV_PART_KNOB);  /* 22 px knob */
  lv_obj_set_style_pad_all(sl, 12, LV_STATE_PRESSED | LV_PART_KNOB);
}

/* speaker (tap: mute) | slider | 42% — under the title, level with the cover's bottom */
static void build_volume() {
  s_vol_row = ui_box(s_root);
  lv_obj_set_size(s_vol_row, COL_W, 44);
  lv_obj_set_pos(s_vol_row, COL_X, VOL_CY - 22);
  lv_obj_t *mute = ui_box(s_vol_row);
  lv_obj_set_size(mute, 40, 44);
  lv_obj_set_pos(mute, -6, 0);
  lv_obj_add_flag(mute, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_add_event_cb(mute, on_vol_mute, LV_EVENT_CLICKED, nullptr);
  s_vol_ico = ui_label(mute, &font_icons_20, COL_TEXT_DIM, icon("vol_up"));
  lv_obj_center(s_vol_ico);
  s_vol_pct = ui_label(s_vol_row, &font_mono_16, COL_TEXT_DIM, "");
  lv_obj_set_width(s_vol_pct, 52);
  lv_obj_set_style_text_align(s_vol_pct, LV_TEXT_ALIGN_RIGHT, 0);
  lv_obj_align(s_vol_pct, LV_ALIGN_RIGHT_MID, 0, 0);
  s_vol_slider = lv_slider_create(s_vol_row);
  style_bar(s_vol_slider, COL_TEXT_DIM);
  lv_obj_set_size(s_vol_slider, COL_W - 40 - 52 - 22, 8);
  lv_obj_align(s_vol_slider, LV_ALIGN_LEFT_MID, 44, 0);
  lv_slider_set_range(s_vol_slider, 0, 100);
  lv_obj_set_ext_click_area(s_vol_slider, 18);
  lv_obj_add_event_cb(s_vol_slider, on_volume, LV_EVENT_ALL, nullptr);
  lv_obj_add_flag(s_vol_row, LV_OBJ_FLAG_HIDDEN);
}

/* Shown while the app is connected and reports a volume; follows it unless the
 * finger is on it (or just let go). */
static void render_volume() {
  bool show = s_ok && agent_alive() && s_pc_vol >= 0;
  if (lv_obj_has_flag(s_vol_row, LV_OBJ_FLAG_HIDDEN) == show) {
    if (show) lv_obj_remove_flag(s_vol_row, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(s_vol_row, LV_OBJ_FLAG_HIDDEN);
  }
  if (!show || s_vol_drag || (int32_t)(s_vol_hold_until - millis()) > 0) return;
  if (lv_slider_get_value(s_vol_slider) != s_pc_vol || !lv_label_get_text(s_vol_pct)[0]) {
    lv_slider_set_value(s_vol_slider, s_pc_vol, LV_ANIM_OFF);
    render_vol(s_pc_vol);
  }
}

static void build_transport() {
  /* shuffle 96 | prev 132 | play 196 | next 132 | repeat 96, gaps 20 = 732 */
  const int g = 20;
  int x = (800 - 732) / 2;
  s_btn_shuffle = toggle_pad(s_root, 96, 72, x, icon("shuffle"), "SHUFFLE", on_shuffle);
  x += 96 + g;
  s_btn_prev = icon_pad(s_root, 132, 84, x, &font_icons_36, icon("prev_s"), on_prev, nullptr);
  x += 132 + g;
  s_btn_play = icon_pad(s_root, 196, 84, x, &font_icons_52, icon("play_s"), on_play, &s_play_ico);
  lv_obj_set_style_bg_opa(s_btn_play, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(s_btn_play, 0, 0);
  ui_set_text_color(s_play_ico, COL_BG);
  x += 196 + g;
  s_btn_next = icon_pad(s_root, 132, 84, x, &font_icons_36, icon("next_s"), on_next, nullptr);
  x += 132 + g;
  s_btn_repeat = toggle_pad(s_root, 96, 72, x, icon("repeat"), "REPEAT", on_repeat);
}

void music_page_build(lv_obj_t *tile) {
  s_built = true;
  s_root = ui_box(tile);
  lv_obj_set_size(s_root, 800, CONTENT_H);

  /* fallback tint until the backdrop for this track arrives */
  s_wash = ui_card(s_root, COL_BG, 0);
  lv_obj_set_size(s_wash, 800, CONTENT_H);
  s_bg = lv_image_create(s_root);
  lv_obj_set_pos(s_bg, 0, 0);
  lv_obj_add_flag(s_bg, LV_OBJ_FLAG_HIDDEN);

  s_art_ph = ui_card(s_root, COL_SURFACE, 16);
  lv_obj_set_size(s_art_ph, ART_PX, ART_PX);
  lv_obj_set_pos(s_art_ph, ART_X, ART_Y);
  s_art_ph_ico = ui_label(s_art_ph, &font_icons_36, COL_TEXT_FAINT, icon("music"));
  lv_obj_center(s_art_ph_ico);
  s_art = lv_image_create(s_root);
  lv_obj_set_pos(s_art, ART_X, ART_Y);
  lv_obj_add_flag(s_art, LV_OBJ_FLAG_HIDDEN);

  /* source row: coloured dot + app name */
  s_dot = ui_card(s_root, COL_CLAY, LV_RADIUS_CIRCLE);
  lv_obj_set_size(s_dot, 8, 8);
  lv_obj_set_pos(s_dot, COL_X, ART_Y + 22);
  s_app = ui_label(s_root, &lv_font_montserrat_14, COL_TEXT_DIM, "");

  s_btn_heart = ui_card(s_root, 0x000000, LV_RADIUS_CIRCLE);
  lv_obj_set_size(s_btn_heart, 52, 52);
  lv_obj_set_pos(s_btn_heart, 760 - 52, ART_Y - 8);
  lv_obj_set_style_bg_opa(s_btn_heart, GLASS_OPA, 0);
  lv_obj_set_style_bg_opa(s_btn_heart, LV_OPA_70, LV_STATE_PRESSED);
  lv_obj_set_style_translate_y(s_btn_heart, 2, LV_STATE_PRESSED);
  lv_obj_add_flag(s_btn_heart, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_ext_click_area(s_btn_heart, 10);
  lv_obj_add_event_cb(s_btn_heart, on_heart, LV_EVENT_CLICKED, nullptr);
  s_heart_ico = ui_label(s_btn_heart, &font_icons_36, COL_TEXT_FAINT, icon("heart"));
  lv_obj_center(s_heart_ico);
  lv_obj_set_style_text_letter_space(s_app, 3, 0);
  lv_obj_set_pos(s_app, COL_X + 16, ART_Y + 16);

  /* title / artist / album stack */
  lv_obj_t *col = ui_box(s_root);
  lv_obj_set_pos(col, COL_X, ART_Y + 44);
  lv_obj_set_size(col, COL_W, ART_PX - 44);
  lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(col, 8, 0);
  s_title = ui_label(col, &font_head_28, COL_TEXT, "");
  lv_obj_set_width(s_title, COL_W);
  lv_obj_set_style_text_line_space(s_title, 2, 0);
  lv_label_set_long_mode(s_title, LV_LABEL_LONG_MODE_DOTS);
  s_artist = ui_label(col, &lv_font_montserrat_20, COL_TEXT, "");
  lv_obj_set_width(s_artist, COL_W);
  ui_one_line(s_artist);
  s_album = ui_label(col, &lv_font_montserrat_16, COL_TEXT_DIM, "");
  lv_obj_set_width(s_album, COL_W);
  ui_one_line(s_album);

  build_slider();
  build_volume();
  build_transport();
  build_empty(tile);
}

/* ---- render ----------------------------------------------------------------------- */
/* Short titles get the big heading; longer ones wrap to two lines at 28 px,
 * then ellipsis. */
static void set_title(const char *t) {
  const char *cur = lv_label_get_text(s_title);
  if (cur && !strcmp(cur, t)) return;
  lv_point_t sz;
  lv_text_get_size(&sz, t, &font_head_48, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
  const lv_font_t *f = sz.x <= COL_W ? &font_head_48 : &font_head_28;
  lv_obj_set_style_text_font(s_title, f, 0);
  int32_t lh = lv_font_get_line_height(f);
  lv_obj_set_height(s_title, LV_SIZE_CONTENT);
  lv_label_set_text(s_title, t);
  lv_obj_update_layout(s_title);
  if (lv_obj_get_height(s_title) > 2 * lh + 2) lv_obj_set_height(s_title, 2 * lh + 2);
}

/* SHUFFLE / REPEAT: tinted glyph, caption and rim when on; hidden when the
 * app doesn't report the state. */
static void style_toggle(lv_obj_t *btn, int8_t state, const char *glyph) {
  if (state < 0) {
    lv_obj_add_flag(btn, LV_OBJ_FLAG_HIDDEN);
    return;
  }
  lv_obj_remove_flag(btn, LV_OBJ_FLAG_HIDDEN);
  lv_obj_t *ico = lv_obj_get_child(btn, 0), *cap = lv_obj_get_child(btn, 1);
  ui_set_text(ico, glyph);
  ui_set_text_color(ico, state ? s_tint : COL_TEXT_DIM);
  ui_set_text_color(cap, state ? s_tint : COL_TEXT_FAINT);
  lv_color_t rim = C(state ? s_tint : 0xFFFFFF);
  lv_opa_t rim_opa = state ? LV_OPA_70 : LV_OPA_10;
  if (!lv_color_eq(lv_obj_get_style_border_color(btn, LV_PART_MAIN), rim) ||
      lv_obj_get_style_border_opa(btn, LV_PART_MAIN) != rim_opa) {
    lv_obj_set_style_border_color(btn, rim, 0);
    lv_obj_set_style_border_opa(btn, rim_opa, 0);
  }
}

static void render_art() {
  /* cover */
  const char *tag = icon_store_tag("art");
  bool have = s_ok && s_m.art[0] && !strcmp(tag, s_m.art);
  const char *want = have ? s_m.art : "";
  if (strcmp(want, s_shown_art) || icon_store_rev() != s_shown_icons_rev) {
    bool changed = strcmp(want, s_shown_art) != 0;
    strncpy(s_shown_art, want, sizeof s_shown_art - 1);
    s_shown_icons_rev = icon_store_rev();
    if (have) {
      if (changed) lv_image_set_src(s_art, icon_store_get("art"));
      lv_obj_remove_flag(s_art, LV_OBJ_FLAG_HIDDEN);
      lv_obj_add_flag(s_art_ph, LV_OBJ_FLAG_HIDDEN);
    } else {
      lv_obj_add_flag(s_art, LV_OBJ_FLAG_HIDDEN);
      lv_obj_remove_flag(s_art_ph, LV_OBJ_FLAG_HIDDEN);
    }
  }
  /* backdrop */
  bool have_bg = s_ok && s_m.art[0] && !strcmp(backdrop_tag(), s_m.art);
  const char *want_bg = have_bg ? s_m.art : "";
  if (strcmp(want_bg, s_shown_bg)) {
    strncpy(s_shown_bg, want_bg, sizeof s_shown_bg - 1);
    if (have_bg) {
      lv_image_set_src(s_bg, backdrop_get());
      lv_obj_remove_flag(s_bg, LV_OBJ_FLAG_HIDDEN);
    } else {
      lv_obj_add_flag(s_bg, LV_OBJ_FLAG_HIDDEN);
    }
  }
}

static void render_tint() {
  uint32_t t = s_m.tint ? s_m.tint : COL_CLAY;
  if (t == s_tint) return;
  s_tint = t;
  lv_obj_set_style_bg_color(s_wash, C(ui_mix(COL_BG, t, 26)), 0);
  lv_obj_set_style_bg_color(s_art_ph, C(ui_mix(COL_SURFACE, t, 40)), 0);
  ui_set_text_color(s_art_ph_ico, ui_mix(COL_TEXT_FAINT, t, 160));
  lv_obj_set_style_bg_color(s_btn_play, C(t), 0);
  lv_obj_set_style_bg_color(s_btn_play, C(ui_mix(t, COL_TEXT, 70)), LV_STATE_PRESSED);
  lv_obj_set_style_bg_color(s_slider, C(t), LV_PART_INDICATOR);
}

/* The dot before the source name ("SPOTIFY"): the service's colour (else the
 * cover tint), dimmed while paused. */
static void render_dot() {
  uint32_t base = s_m.app_color ? s_m.app_color : (s_tint ? s_tint : COL_CLAY);
  uint32_t want = music_playing() ? base : ui_mix(COL_BG, base, 110);
  if (!lv_color_eq(lv_obj_get_style_bg_color(s_dot, LV_PART_MAIN), C(want)))
    lv_obj_set_style_bg_color(s_dot, C(want), 0);
}

static void render_play() {
  render_dot();
  int p = music_playing() ? 1 : 0;
  if (p == s_shown_playing) return;
  s_shown_playing = p;
  ui_set_text(s_play_ico, icon(p ? "pause_s" : "play_s"));
}

static void render_progress() {
  bool has_dur = s_m.dur_s > 0;
  if (has_dur == lv_obj_has_flag(s_slider, LV_OBJ_FLAG_HIDDEN)) {
    if (has_dur) {
      lv_obj_remove_flag(s_slider, LV_OBJ_FLAG_HIDDEN);
      lv_obj_remove_flag(s_dur, LV_OBJ_FLAG_HIDDEN);
    } else {
      lv_obj_add_flag(s_slider, LV_OBJ_FLAG_HIDDEN);
      lv_obj_add_flag(s_dur, LV_OBJ_FLAG_HIDDEN);
    }
  }
  if (s_scrubbing) return;
  int32_t p = live_pos();
  if (p == s_shown_sec) return;
  s_shown_sec = p;
  char b[16];
  fmt_time(b, sizeof b, p);
  ui_set_text(s_pos, b);
  if (has_dur) {
    fmt_time(b, sizeof b, s_m.dur_s);
    ui_set_text(s_dur, b);
    if (lv_slider_get_max_value(s_slider) != s_m.dur_s) lv_slider_set_range(s_slider, 0, s_m.dur_s);
    if (lv_slider_get_value(s_slider) != p) lv_slider_set_value(s_slider, p, LV_ANIM_OFF);
  }
}

static void show_empty(bool empty, bool agent_ok) {
  if (empty) {
    lv_obj_add_flag(s_root, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s_empty, LV_OBJ_FLAG_HIDDEN);
    ui_set_text(s_empty_sub, agent_ok ? "Play something in Spotify on the PC" : "PC agent offline");
  } else {
    lv_obj_remove_flag(s_root, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_empty, LV_OBJ_FLAG_HIDDEN);
  }
}

void music_page_update(const Model &m, bool agent_ok) {
  const MediaInfo &n = m.media;
  s_pc_vol = m.pc.vol;
  bool ok = agent_ok && n.ok;
  bool new_track = strcmp(n.title, s_m.title) || strcmp(n.artist, s_m.artist);
  bool state_flip = ok && (n.playing != s_m.playing);
  int32_t drift = live_pos() - n.pos_s;
  if (drift < 0) drift = -drift;

  if (ok) {
    s_m = n;
  } else {
    memset(&s_m, 0, sizeof s_m);
    s_m.shuffle = s_m.repeat = -1;
  }
  s_ok = ok;
  if (s_opt && model_playing() == s_opt_playing) s_opt = false; /* confirmed */
  if (s_opt_liked != -2 && s_m.liked == s_opt_liked) s_opt_liked = -2;

  if (ok && (new_track || state_flip || drift > RESYNC_DRIFT_S)) rebase(n.pos_s);

  if (!s_built) return; /* state only: the deck mirrors music_playing() */
  show_empty(!ok, agent_ok);
  if (!ok) {
    render_art();
    render_volume(); /* hides it */
    return;
  }
  char app[24];
  snprintf(app, sizeof app, "%s", n.app[0] ? n.app : "Media");
  for (char *c = app; *c; c++)
    if (*c >= 'a' && *c <= 'z') *c -= 32;
  ui_set_text(s_app, app);
  set_title(n.title[0] ? n.title : "Unknown title");
  ui_set_text(s_artist, n.artist);
  ui_set_text(s_album, n.album);
  render_tint();
  render_art();
  style_toggle(s_btn_shuffle, n.shuffle, icon("shuffle"));
  style_toggle(s_btn_repeat, n.repeat, n.repeat == 1 ? icon("repeat_one") : icon("repeat"));
  render_play();
  render_heart();
  render_volume();
  render_progress();
}

/* Called on every UI refresh tick (250 ms): clock + optimistic state. */
static void render_heart() {
  int8_t l = liked_now();
  if ((l < 0) != lv_obj_has_flag(s_btn_heart, LV_OBJ_FLAG_HIDDEN)) {
    if (l < 0) lv_obj_add_flag(s_btn_heart, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_remove_flag(s_btn_heart, LV_OBJ_FLAG_HIDDEN);
  }
  if (l < 0) return;
  ui_set_text(s_heart_ico, icon(l == 1 ? "heart_s" : "heart"));
  ui_set_text_color(s_heart_ico, l == 1 ? (s_m.app_color ? s_m.app_color : 0x1DB954) : l == 0 ? COL_TEXT
                                                                                              : COL_TEXT_FAINT);
}

void music_page_tick() {
  if (!s_built || !s_ok) return;
  render_play();
  render_heart();
  render_volume();
  render_progress();
  render_art(); /* the cover / backdrop may land a moment after the title */
}
