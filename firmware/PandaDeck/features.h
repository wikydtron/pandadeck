/* Build-time options.
 *
 * config.h is OPTIONAL (gitignored; template: config.h.example). Without it
 * the firmware is the generic build: no Wi-Fi credentials or token compiled
 * in. The panel then generates its own token on first boot, takes Wi-Fi
 * settings from the desktop app (USB) and otherwise re-uses whatever network
 * the ESP32's Wi-Fi storage already knows. */
#pragma once

#if defined(__has_include)
#if __has_include("config.h")
#include "config.h"
#endif
#endif

/* mDNS / DHCP host name: http://pandadeck.local */
#ifndef PANDA_HOSTNAME
#define PANDA_HOSTNAME "pandadeck"
#endif

/* POSIX TZ used until the app sets one (settings "tz"). */
#ifndef PANDA_TZ
#define PANDA_TZ "UTC0"
#endif

