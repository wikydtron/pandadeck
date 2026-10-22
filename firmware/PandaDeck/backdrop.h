/* MUSIC page backdrop: the agent sends a small blurred, darkened RGB565 copy
 * of the cover (POST /api/backdrop, e.g. 200x108); it is scaled up once
 * (bilinear + ordered dither, so dark gradients don't band) into a
 * full-page 800x432 image in PSRAM. Two buffers ping-pong so the UI never
 * draws a half-written frame. */
#pragma once
#include <lvgl.h>
#include <stddef.h>
#include <stdint.h>

#define BACKDROP_W 800
#define BACKDROP_H 432
#define BACKDROP_SRC_MAX_W 400
#define BACKDROP_SRC_MAX_H 216
#define BACKDROP_TAG_LEN 12

/* Link task (HTTP or USB, via api.cpp) */
bool backdrop_upload_begin(uint16_t w, uint16_t h, const char *tag);
/* Allocates the staging buffer now (see icon_upload_reserve). */
bool backdrop_upload_reserve();
void backdrop_upload_write(const uint8_t *data, size_t len);
bool backdrop_upload_commit();

/* UI task: the current backdrop (nullptr until the first upload) and its tag. */
const lv_image_dsc_t *backdrop_get();
const char *backdrop_tag();
