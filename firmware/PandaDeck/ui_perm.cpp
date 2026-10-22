/* Permission takeover: when Claude Code's PermissionRequest hook is waiting
 * on the PC agent, the oldest request covers whatever page is showing.
 * Approve/Deny queue an event for the agent; the request id is remembered
 * locally so it doesn't flash back before the agent drops it. */
#include "store.h"
#include "ui_pages.h"
#include "ui_theme.h"

#include <Arduino.h>
#include <stdio.h>
#include <string.h>

#define DECIDED_MAX 8
#define DECIDED_TTL_MS 60000

static lv_obj_t *s_root, *s_card, *s_count, *s_where, *s_tool, *s_detail, *s_age, *s_lead;
static char s_shown_id[24] = "";
static int32_t s_shown_age = 0;
static uint32_t s_shown_at = 0;

struct Decided {
  char id[24];
  uint32_t at;
};
static Decided s_decided[DECIDED_MAX];

static bool was_decided(const char *id) {
  for (Decided &d : s_decided)
    if (d.id[0] && millis() - d.at < DECIDED_TTL_MS && !strcmp(d.id, id)) return true;
  return false;
}

static void remember(const char *id) {
  Decided *slot = &s_decided[0];
  for (Decided &d : s_decided) {
    if (!d.id[0] || millis() - d.at >= DECIDED_TTL_MS) { slot = &d; break; }
    if (d.at < slot->at) slot = &d;
  }
  strncpy(slot->id, id, sizeof slot->id - 1);
  slot->id[sizeof slot->id - 1] = '\0';
  slot->at = millis();
}

#define ARM_DELAY_MS 600

static void decide(const char *verdict) {
  if (!s_shown_id[0]) return;
  /* The takeover can appear under a finger that was mid-tap on another page:
   * ignore taps until it has been on screen long enough to be seen. */
  if (millis() - s_shown_at < ARM_DELAY_MS) return;
  store_push_event("perm", s_shown_id, verdict);
  remember(s_shown_id);
  s_shown_id[0] = '\0';
  lv_obj_add_flag(s_root, LV_OBJ_FLAG_HIDDEN);
}

static void on_allow(lv_event_t *) { decide("allow"); }
static void on_deny(lv_event_t *) { decide("deny"); }

static lv_obj_t *make_button(lv_obj_t *parent, int x, int w, uint32_t col, bool filled, const char *ico,
                             const char *txt, lv_event_cb_t cb) {
  lv_obj_t *b = ui_card(parent, filled ? col : COL_SURFACE, 16);
  lv_obj_set_size(b, w, 92);
  lv_obj_set_pos(b, x, 300);
  lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
  if (!filled) {
    lv_obj_set_style_border_width(b, 2, 0);
    lv_obj_set_style_border_color(b, C(col), 0);
  }
  lv_obj_set_style_bg_color(b, C(filled ? ui_mix(col, 0xFFFFFF, 50) : ui_mix(COL_SURFACE, col, 70)),
                            LV_STATE_PRESSED);
  lv_obj_set_style_translate_y(b, 2, LV_STATE_PRESSED);
  uint32_t fg = filled ? COL_BG : col;
  lv_obj_t *row = ui_box(b);
  lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
  lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_set_style_pad_column(row, 14, 0);
  lv_obj_center(row);
  ui_label(row, &font_icons_20, fg, icon(ico));
  lv_obj_t *l = ui_label(row, &font_head_28, fg, txt);
  (void)l;
  lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, nullptr);
  return b;
}

void perm_overlay_build() {
  s_root = ui_box(lv_layer_top());
  lv_obj_set_size(s_root, 800, 480);
  /* Opaque backdrop: with a translucent scrim LVGL has to render the whole
   * page underneath on every frame, which made the takeover slow to appear
   * and to respond. */
  lv_obj_set_style_bg_color(s_root, C(0x000000), 0);
  lv_obj_set_style_bg_opa(s_root, LV_OPA_COVER, 0);
  lv_obj_add_flag(s_root, LV_OBJ_FLAG_CLICKABLE);

  s_card = ui_card(s_root, COL_SURFACE, 22);
  lv_obj_set_size(s_card, 760, 440);
  lv_obj_center(s_card);
  lv_obj_set_style_border_width(s_card, 2, 0);
  lv_obj_set_style_border_color(s_card, C(COL_AMBER), 0);

  lv_obj_t *chip = ui_card(s_card, COL_AMBER, 12);
  lv_obj_set_size(chip, LV_SIZE_CONTENT, 28);
  lv_obj_set_style_pad_hor(chip, 12, 0);
  lv_obj_set_pos(chip, 28, 24);
  lv_obj_t *cl = ui_label(chip, &lv_font_montserrat_14, COL_BG, "PERMISSION");
  lv_obj_set_style_text_letter_space(cl, 2, 0);
  lv_obj_center(cl);

  s_where = ui_label(s_card, &lv_font_montserrat_16, COL_TEXT_DIM, "");
  lv_obj_set_pos(s_where, 170, 29);
  s_count = ui_label(s_card, &lv_font_montserrat_16, COL_TEXT_FAINT, "");
  lv_obj_align(s_count, LV_ALIGN_TOP_RIGHT, -28, 29);

  s_lead = ui_label(s_card, &lv_font_montserrat_18, COL_TEXT_DIM, "Claude wants to use");
  lv_obj_set_pos(s_lead, 28, 70);
  s_tool = ui_label(s_card, &font_head_48, COL_TEXT, "");
  lv_obj_set_pos(s_tool, 26, 94);
  lv_obj_set_width(s_tool, 700);
  ui_one_line(s_tool);

  lv_obj_t *box = ui_card(s_card, COL_BG, 12);
  lv_obj_set_pos(box, 28, 158);
  lv_obj_set_size(box, 704, 128);
  lv_obj_add_flag(box, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_scroll_dir(box, LV_DIR_VER);
  lv_obj_set_style_pad_all(box, 14, 0);
  s_detail = ui_label(box, &font_mono_20, COL_TEXT, "");
  lv_obj_set_width(s_detail, 676);
  lv_label_set_long_mode(s_detail, LV_LABEL_LONG_MODE_WRAP);

  make_button(s_card, 28, 290, COL_RED, false, "close", "Deny", on_deny);
  make_button(s_card, 334, 398, COL_GREEN, true, "check", "Approve", on_allow);

  s_age = ui_label(s_card, &lv_font_montserrat_14, COL_TEXT_FAINT, "");
  lv_obj_align(s_age, LV_ALIGN_TOP_RIGHT, -28, 70);

  lv_obj_add_flag(s_root, LV_OBJ_FLAG_HIDDEN);
}

bool perm_overlay_update(const Model &m) {
  const PermInfo *pick = nullptr;
  int pending = 0;
  for (int i = 0; i < m.n_perms; i++) {
    if (!m.perms[i].id[0] || was_decided(m.perms[i].id)) continue;
    pending++;
    if (!pick) pick = &m.perms[i];
  }
  if (!pick) {
    s_shown_id[0] = '\0';
    lv_obj_add_flag(s_root, LV_OBJ_FLAG_HIDDEN);
    return false;
  }
  bool fresh = strcmp(pick->id, s_shown_id) != 0;
  if (fresh) {
    s_shown_at = millis();
    strncpy(s_shown_id, pick->id, sizeof s_shown_id - 1);
    s_shown_id[sizeof s_shown_id - 1] = '\0';
    ui_set_text(s_lead, pick->src == SRC_CODEX ? "Codex wants to use" : "Claude wants to use");
    ui_set_text(s_tool, pick->tool[0] ? pick->tool : "a tool");
    ui_set_text(s_detail, pick->detail);
    lv_obj_scroll_to_y(lv_obj_get_parent(s_detail), 0, LV_ANIM_OFF);
    char w[96];
    snprintf(w, sizeof w, "%s%s%s", pick->proj, pick->host[0] ? "  \xE2\x80\xA2  " : "", pick->host);
    ui_set_text(s_where, w);
    lv_obj_remove_flag(s_root, LV_OBJ_FLAG_HIDDEN);
  }
  s_shown_age = pick->age_s;
  char b[32];
  if (pending > 1) snprintf(b, sizeof b, "1 of %d", pending);
  else b[0] = '\0';
  ui_set_text(s_count, b);
  char d[16];
  ui_fmt_dur(d, sizeof d, s_shown_age);
  snprintf(b, sizeof b, "waiting %s", d);
  ui_set_text(s_age, b);
  return fresh;
}
