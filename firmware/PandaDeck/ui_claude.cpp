/* AI page — mission control: live Claude + Codex sessions (left), usage (right).
 * Claude sessions are clay, Codex sessions blue (same rule as the corner mark). */
#include "ui_pages.h"
#include "ui_theme.h"

#include <stdio.h>
#include <string.h>

/* Claude Code's own spinner, forward then back. */
static const char *SPIN[] = {"\xC2\xB7", "\xE2\x9C\xA2", "\xE2\x9C\xB3", "\xE2\x9C\xB6", "\xE2\x9C\xBB",
                             "\xE2\x9C\xBD", "\xE2\x9C\xBB", "\xE2\x9C\xB6", "\xE2\x9C\xB3", "\xE2\x9C\xA2"};
static const int N_SPIN = sizeof(SPIN) / sizeof(SPIN[0]);

struct Card {
  lv_obj_t *root, *rail, *glyph, *proj, *meta, *act;
  uint8_t st, src;
};

static Card s_cards[MODEL_MAX_SESSIONS];
static lv_obj_t *s_list;
static lv_obj_t *s_empty, *s_empty_glyph, *s_empty_title, *s_empty_sub;
static lv_obj_t *s_pct_5h, *s_bar_5h, *s_reset_5h;
static lv_obj_t *s_pct_wk, *s_bar_wk, *s_reset_wk;
static lv_obj_t *s_pct_cx, *s_bar_cx, *s_reset_cx;
static lv_obj_t *s_n_work, *s_n_wait;
static int s_spin = 0;

static uint32_t state_color(uint8_t st, uint8_t src) {
  switch (st) {
    case SESS_WORKING: return src == SRC_CODEX ? COL_BLUE : COL_CLAY;
    case SESS_WAITING: return COL_AMBER;
    case SESS_PERMISSION: return COL_RED;
    default: return COL_TEXT_FAINT;
  }
}

static void build_card(Card &c) {
  c.root = ui_card(s_list, COL_SURFACE, 14);
  lv_obj_set_size(c.root, 528, 86);

  c.rail = ui_card(c.root, COL_TEXT_FAINT, 3);
  lv_obj_set_size(c.rail, 5, 62);
  lv_obj_align(c.rail, LV_ALIGN_LEFT_MID, 8, 0);

  c.glyph = ui_label(c.root, &font_mono_20, COL_CLAY, "");
  lv_obj_set_pos(c.glyph, 24, 14);

  c.proj = ui_label(c.root, &font_head_28, COL_TEXT, "");
  lv_obj_set_pos(c.proj, 54, 8);
  lv_obj_set_width(c.proj, 320);
  ui_one_line(c.proj);

  c.meta = ui_label(c.root, &lv_font_montserrat_14, COL_TEXT_DIM, "");
  lv_obj_set_width(c.meta, 140);
  lv_obj_set_style_text_align(c.meta, LV_TEXT_ALIGN_RIGHT, 0);
  lv_obj_align(c.meta, LV_ALIGN_TOP_RIGHT, -16, 16);

  c.act = ui_label(c.root, &font_mono_16, COL_TEXT_DIM, "");
  lv_obj_set_pos(c.act, 24, 54);
  lv_obj_set_width(c.act, 488);
  ui_one_line(c.act);

  c.st = 0xFF;
  lv_obj_add_flag(c.root, LV_OBJ_FLAG_HIDDEN);
}

static void set_card(Card &c, const SessionInfo &s) {
  uint32_t col = state_color(s.st, s.src);
  if (c.st != s.st || c.src != s.src) {
    c.st = s.st;
    c.src = s.src;
    lv_obj_set_style_bg_color(c.rail, C(col), 0);
    lv_obj_set_style_text_color(c.glyph, C(col), 0);
    if (s.st == SESS_WORKING) {
      lv_obj_set_style_text_font(c.glyph, &font_mono_20, 0);
    } else {
      lv_obj_set_style_text_font(c.glyph, &font_icons_20, 0);
      ui_set_text(c.glyph, s.st == SESS_WAITING      ? icon("enter")
                                 : s.st == SESS_PERMISSION ? icon("lock")
                                                           : icon("check"));
    }
    /* Waiting sessions get a warm outline so they read from across the room. */
    lv_obj_set_style_border_width(c.root, s.st == SESS_WAITING || s.st == SESS_PERMISSION ? 2 : 0, 0);
    lv_obj_set_style_border_color(c.root, C(col), 0);
    lv_obj_set_style_border_opa(c.root, LV_OPA_60, 0);
  }
  ui_set_text(c.proj, s.proj[0] ? s.proj : (s.title[0] ? s.title : "session"));
  char meta[48], dur[16];
  ui_fmt_dur(dur, sizeof dur, s.dur_s);
  const char *who = s.model[0] ? s.model : (s.src == SRC_CODEX ? "codex" : "claude");
  snprintf(meta, sizeof meta, "%s  \xE2\x80\xA2  %s", who, dur);
  ui_set_text(c.meta, meta);
  ui_set_text(c.act, s.act[0] ? s.act : (s.title[0] ? s.title : ""));
  ui_set_text_color(c.act, s.st == SESS_IDLE ? COL_TEXT_FAINT : COL_TEXT_DIM);
  lv_obj_remove_flag(c.root, LV_OBJ_FLAG_HIDDEN);
}

static void spin_timer(lv_timer_t *) {
  s_spin = (s_spin + 1) % N_SPIN;
  for (Card &c : s_cards) {
    if (c.st == SESS_WORKING && !lv_obj_has_flag(c.root, LV_OBJ_FLAG_HIDDEN))
      ui_set_text(c.glyph, SPIN[s_spin]);
  }
  if (!lv_obj_has_flag(s_empty, LV_OBJ_FLAG_HIDDEN))
    ui_set_text(s_empty_glyph, SPIN[(s_spin / 2) % N_SPIN]);
}

/* A "42%" numeral + source tag + window caption + bar + reset line. All three
 * meters (Claude 5-hour, Claude weekly, Codex weekly) use the same block so
 * they read at the same size. Returns the next y. */
static int build_usage_block(lv_obj_t *col, int y, const char *src, uint32_t src_color, const char *caption,
                             lv_obj_t **pct, lv_obj_t **bar, lv_obj_t **reset) {
  *pct = ui_label(col, &font_serif_56, COL_TEXT, "--");
  lv_obj_set_pos(*pct, 0, y);
  lv_obj_t *tag = ui_label(col, &lv_font_montserrat_12, src_color, src);
  lv_obj_set_style_text_letter_space(tag, 2, 0);
  lv_obj_align(tag, LV_ALIGN_TOP_RIGHT, 0, y + 14);
  lv_obj_t *cap = ui_label(col, &lv_font_montserrat_14, COL_TEXT_DIM, caption);
  lv_obj_set_style_text_letter_space(cap, 2, 0);
  lv_obj_align(cap, LV_ALIGN_TOP_RIGHT, 0, y + 32);
  *bar = ui_bar(col, 224, 6, COL_GREEN);
  lv_obj_set_pos(*bar, 0, y + 66);
  *reset = ui_label(col, &lv_font_montserrat_14, COL_TEXT_FAINT, "");
  lv_obj_set_pos(*reset, 0, y + 76);
  return y + 100;
}

static lv_obj_t *build_counter(lv_obj_t *col, int x, int y, uint32_t color, const char *caption) {
  lv_obj_t *n = ui_label(col, &font_head_48, color, "0");
  lv_obj_set_pos(n, x, y);
  lv_obj_t *cap = ui_label(col, &lv_font_montserrat_14, COL_TEXT_DIM, caption);
  lv_obj_set_pos(cap, x + 2, y + 54);
  return n;
}

void claude_page_build(lv_obj_t *tile) {
  s_list = ui_box(tile);
  lv_obj_set_pos(s_list, 16, 10);
  lv_obj_set_size(s_list, 528, CONTENT_H - 20);
  lv_obj_add_flag(s_list, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_scroll_dir(s_list, LV_DIR_VER);
  lv_obj_set_scrollbar_mode(s_list, LV_SCROLLBAR_MODE_OFF);
  lv_obj_set_flex_flow(s_list, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(s_list, 10, 0);
  for (Card &c : s_cards) build_card(c);

  s_empty = ui_box(tile);
  lv_obj_set_pos(s_empty, 16, 10);
  lv_obj_set_size(s_empty, 528, CONTENT_H - 20);
  s_empty_glyph = ui_label(s_empty, &font_mono_32, COL_CLAY, SPIN[4]);
  lv_obj_align(s_empty_glyph, LV_ALIGN_CENTER, 0, -64);
  s_empty_title = ui_label(s_empty, &font_head_28, COL_TEXT, "All quiet");
  lv_obj_align(s_empty_title, LV_ALIGN_CENTER, 0, -10);
  s_empty_sub = ui_label(s_empty, &lv_font_montserrat_16, COL_TEXT_DIM,
                         "Claude and Codex sessions on your computer show up here.");
  lv_obj_align(s_empty_sub, LV_ALIGN_CENTER, 0, 28);

  lv_obj_t *col = ui_box(tile);
  lv_obj_set_pos(col, 560, 10);
  lv_obj_set_size(col, 224, CONTENT_H - 20);
  int y = build_usage_block(col, 2, "CLAUDE", COL_CLAY, "5-HOUR", &s_pct_5h, &s_bar_5h, &s_reset_5h);
  y = build_usage_block(col, y, "CLAUDE", COL_CLAY, "WEEKLY", &s_pct_wk, &s_bar_wk, &s_reset_wk);
  y = build_usage_block(col, y, "CODEX", COL_BLUE, "WEEKLY", &s_pct_cx, &s_bar_cx, &s_reset_cx);

  lv_obj_t *rule = ui_card(col, COL_LINE, 0);
  lv_obj_set_size(rule, 224, 1);
  lv_obj_set_pos(rule, 0, y + 4);
  s_n_work = build_counter(col, 0, y + 12, COL_CLAY, "working");
  s_n_wait = build_counter(col, 118, y + 12, COL_AMBER, "need you");

  lv_timer_create(spin_timer, 110, nullptr);
}

static void set_usage(lv_obj_t *pct, lv_obj_t *bar, lv_obj_t *reset, float v, int32_t reset_s) {
  char b[32];
  if (v < 0) {
    ui_set_text(pct, "--");
    ui_bar_set(bar, 0, COL_TEXT_FAINT);
  } else {
    snprintf(b, sizeof b, "%d%%", (int)(v + 0.5f));
    ui_set_text(pct, b);
    ui_bar_set(bar, v, ui_level_color(v, 60, 85));
  }
  if (reset) {
    if (reset_s >= 0) {
      char d[16];
      ui_fmt_dur(d, sizeof d, reset_s);
      snprintf(b, sizeof b, "resets in %s", d);
      ui_set_text(reset, b);
    } else {
      ui_set_text(reset, "");
    }
  }
}

void claude_page_update(const Model &m, bool agent_ok) {
  /* Needs-you first, then working, then the rest (agent sends newest-first
   * within each group). */
  static const uint8_t ORDER[] = {SESS_PERMISSION, SESS_WAITING, SESS_WORKING, SESS_IDLE};
  int shown = 0;
  for (uint8_t st : ORDER) {
    for (int i = 0; i < m.n_sessions && shown < MODEL_MAX_SESSIONS; i++) {
      if (m.sessions[i].st != st) continue;
      set_card(s_cards[shown], m.sessions[i]);
      lv_obj_move_to_index(s_cards[shown].root, shown);
      shown++;
    }
  }
  for (int i = shown; i < MODEL_MAX_SESSIONS; i++) {
    lv_obj_add_flag(s_cards[i].root, LV_OBJ_FLAG_HIDDEN);
    s_cards[i].st = 0xFF;
  }

  if (shown == 0) {
    lv_obj_remove_flag(s_empty, LV_OBJ_FLAG_HIDDEN);
    ui_set_text(s_empty_title, agent_ok ? "All quiet" : "PC agent offline");
    ui_set_text(s_empty_sub, agent_ok ? "Claude and Codex sessions on your computer show up here."
                                      : "Start the PandaDeck app on your computer.");
    lv_obj_set_style_text_color(s_empty_glyph, C(agent_ok ? COL_CLAY : COL_TEXT_FAINT), 0);
  } else {
    lv_obj_add_flag(s_empty, LV_OBJ_FLAG_HIDDEN);
  }

  const UsageInfo &u = m.usage;
  set_usage(s_pct_5h, s_bar_5h, s_reset_5h, u.ok ? u.s5h : -1, u.ok ? u.s5h_reset_s : -1);
  set_usage(s_pct_wk, s_bar_wk, s_reset_wk, u.ok ? u.week : -1, u.ok ? u.week_reset_s : -1);
  set_usage(s_pct_cx, s_bar_cx, s_reset_cx, u.ok ? u.codex_week : -1, u.ok ? u.codex_reset_s : -1);

  char b[8];
  snprintf(b, sizeof b, "%d", model_count_state(m, SESS_WORKING));
  ui_set_text(s_n_work, b);
  snprintf(b, sizeof b, "%d", model_count_state(m, SESS_WAITING) + model_count_state(m, SESS_PERMISSION));
  ui_set_text(s_n_wait, b);
}
