/* Panel settings persisted in NVS (namespace "pd"): pages, Wi-Fi the app
 * stored, brightness, name, time zone, and the API token. Display flip /
 * touch flip live in display.cpp's own NVS namespace ("disp") and are only
 * mirrored here. Thread-safe: written by the link task, read by the UI. */
#pragma once
#include "proto.h"

void settings_init(); /* before net_init() and ui_init() */

/* Current values (flip/tflip reflect the display, including a flip that was
 * requested but not drawn yet). */
Settings settings_get();
/* Bumps on every change; pages_rev only when the tab list changes. */
uint32_t settings_rev();
uint32_t settings_pages_rev();

/* Persists the present keys and applies brightness / flips / pages / name /
 * tz live (the UI picks them up on its next tick). Wi-Fi is persisted only:
 * the caller reconnects (net_apply_wifi) after it has replied. */
void settings_apply(const SettingsPatch &p);

/* Token for the HTTP API (X-Panda-Token): config.h PANDA_TOKEN if the build
 * has one, else 24 random characters generated once and kept in NVS. */
const char *settings_token();
bool settings_token_from_build();

/* Erases namespace "pd" and the display's flip keys (nothing else in NVS:
 * the stock firmware keeps its data in the same partition). Reboot after. */
void settings_factory_reset();
