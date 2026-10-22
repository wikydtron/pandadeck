/* Internal interface between the UI shell (ui.cpp) and its pages. A page is
 * built (once) only when the settings list it; build() gets a full-page
 * container that the shell moves between tiles when the tab order changes. */
#pragma once
#include "features.h"
#include "model.h"
#include <lvgl.h>

void claude_page_build(lv_obj_t *tile);
void claude_page_update(const Model &m, bool agent_ok);

void deck_page_build(lv_obj_t *tile);
void deck_page_set_layout(const DeckLayout &d);
void deck_page_update(const Model &m);

void music_page_build(lv_obj_t *tile);
/* Safe to call before/without build: keeps the play state the deck's
 * play/pause button mirrors. */
void music_page_update(const Model &m, bool agent_ok);
void music_page_tick(); /* every UI refresh: local clock, optimistic state */
/* Effective play state (optimistic for a moment after a tap), shared with
 * the deck's play/pause button. */
bool music_playing();
void music_toggle_optimistic();


void hud_page_build(lv_obj_t *tile);
void hud_page_update(const Model &m, bool agent_ok);
/* Push one sample into the sparklines (called on a fixed cadence). */
void hud_page_sample(const Model &m);

void perm_overlay_build();
/* Shows the oldest undecided request (or hides). Returns true if a request
 * that was not on screen before just appeared (caller wakes the display). */
bool perm_overlay_update(const Model &m);
