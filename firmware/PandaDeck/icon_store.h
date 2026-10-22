/* Images pushed by the PC agent (POST /api/icon): app icons (56 px) and the
 * MUSIC page's album cover (key "art", 320 px). Kept in PSRAM as LVGL
 * RGB565A8 images (RGB565 plane followed by an 8-bit alpha plane). */
#pragma once
#include <lvgl.h>
#include <stddef.h>
#include <stdint.h>

#define ICON_MAX 48
#define ICON_KEY_LEN 16
#define ICON_TAG_LEN 12
#define ICON_MAX_DIM 320

/* Link task (HTTP or USB, via api.cpp): stage an incoming upload. begin()
 * validates size, then write() appends chunks, then commit() publishes it
 * under `key`. `tag` is an opaque version label (the cover's hash) the UI
 * can match against its model. */
bool icon_upload_begin(const char *key, uint16_t w, uint16_t h, const char *tag);
/* Allocates the staging buffer now (the USB link begins uploads from a
 * small-stack callback that shouldn't be the one to allocate it). */
bool icon_upload_reserve();
void icon_upload_write(const uint8_t *data, size_t len);
/* nullptr once published, else why not: "incomplete image", "out of memory",
 * "too many images". A refused upload leaves the previous image on screen. */
const char *icon_upload_commit();

/* UI task: image for a key, or nullptr. The descriptor stays valid forever. */
const lv_image_dsc_t *icon_store_get(const char *key);
/* Tag the image under `key` was uploaded with ("" if none / no image). */
const char *icon_store_tag(const char *key);
/* Bumps whenever any icon changes (UI re-renders the deck). */
uint32_t icon_store_rev();
int icon_store_count();
