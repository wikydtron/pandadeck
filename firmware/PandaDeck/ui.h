#pragma once

/* Builds the whole UI (top bar, three swipeable pages, overlays) and starts
 * the LVGL timers that pull snapshots from the store. Call after
 * display_init() and store_init(). */
void ui_init();
