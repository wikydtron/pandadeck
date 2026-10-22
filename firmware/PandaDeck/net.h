#pragma once
#include <stddef.h>
#include <stdint.h>

#define FW_VERSION "0.6.5"

/* Starts Wi-Fi (non-blocking, never required), NTP and the link task that
 * serves both the USB link (usb_link.cpp) and, once Wi-Fi is up, the HTTP
 * server on port 80:
 *   GET  /              status page
 *   GET  /update        firmware upload page (browser)
 *   POST /update        multipart firmware upload  [token: header, ?token= or form field]
 *   GET  /api/info      panel info (+ diagnostics; SSID only with the token)
 *   POST /api/sync      snapshot in, events out    [token]
 *   POST /api/config    settings update            [token]
 *   POST /api/icon      multipart RGB565A8 icon    [token]
 *   POST /api/backdrop  multipart RGB565 backdrop  [token]
 *   GET  /api/screen    framebuffer dump           [token]
 *   POST /api/tap       inject a touch             [token]
 * The JSON API takes the token only as the X-Panda-Token header.
 *
 * Wi-Fi credentials, first match wins: the app's (NVS settings) > config.h
 * WIFI_SSID/WIFI_PASS > whatever the ESP32's Wi-Fi storage already holds
 * (e.g. left there by the stock firmware). They are never written to that
 * storage, so the stock firmware's network survives. */
void net_init();

bool net_connected();
int net_rssi();
/* Local time available (NTP or the app's clock). */
bool net_time_ok();
/* Some credentials exist (any source). */
bool net_wifi_configured();
/* "app" | "build" | "stored" | "none" */
const char *net_wifi_source();
/* Dotted IP into buf ("" when not connected). */
bool net_ip(char *buf, size_t n);
/* SSID in use / configured ("" if none). */
void net_ssid(char *buf, size_t n);
const char *net_hostname(); /* "pandadeck" -> pandadeck.local */

/* Reconnect with the current settings (after a Wi-Fi change). */
void net_apply_wifi();
/* Friendly name for mDNS service discovery. */
void net_set_name(const char *name);

/* Call from loop(): 20 s after boot, once the panel is reachable (Wi-Fi up
 * or a USB host talked to it), cancels a pending bootloader rollback so
 * this image becomes the permanent one. */
void net_mark_valid_if_healthy();
bool net_marked_valid();
/* Unused stack of the link task (bytes), for /api/info. */
uint32_t net_link_stack_free();
