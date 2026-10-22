#include "backdrop.h"

#include <Arduino.h>
#include <esp_heap_caps.h>
#include <string.h>

static uint8_t *s_buf[2];  /* PSRAM, BACKDROP_W * BACKDROP_H * 2 each */
static int s_cur = -1;     /* buffer on screen, -1 = none yet */
static lv_image_dsc_t s_dsc;
static char s_tag[BACKDROP_TAG_LEN];

/* staging (link task only, one upload at a time: api.cpp) */
static uint8_t *s_st = nullptr;
static uint16_t s_sw, s_sh;
static size_t s_len, s_need;
static char s_st_tag[BACKDROP_TAG_LEN];

bool backdrop_upload_reserve() {
  if (!s_st) s_st = (uint8_t *)heap_caps_malloc(BACKDROP_SRC_MAX_W * BACKDROP_SRC_MAX_H * 2, MALLOC_CAP_SPIRAM);
  return s_st != nullptr;
}

bool backdrop_upload_begin(uint16_t w, uint16_t h, const char *tag) {
  if (w < 2 || h < 2 || w > BACKDROP_SRC_MAX_W || h > BACKDROP_SRC_MAX_H) return false;
  if (!s_st) s_st = (uint8_t *)heap_caps_malloc(BACKDROP_SRC_MAX_W * BACKDROP_SRC_MAX_H * 2, MALLOC_CAP_SPIRAM);
  if (!s_st) return false;
  s_sw = w;
  s_sh = h;
  s_need = (size_t)w * h * 2;
  s_len = 0;
  strncpy(s_st_tag, tag ? tag : "", BACKDROP_TAG_LEN - 1);
  s_st_tag[BACKDROP_TAG_LEN - 1] = '\0';
  return true;
}

void backdrop_upload_write(const uint8_t *data, size_t len) {
  if (!s_st || s_len >= s_need) return;
  size_t n = len < s_need - s_len ? len : s_need - s_len;
  memcpy(s_st + s_len, data, n);
  s_len += n;
}

/* 4x4 Bayer matrix, scaled to the 8->5/6 bit truncation steps. */
static const uint8_t BAYER[4][4] = {{0, 8, 2, 10}, {12, 4, 14, 6}, {3, 11, 1, 9}, {15, 7, 13, 5}};

static inline void unpack(uint16_t v, int &r, int &g, int &b) {
  r = (v >> 11) & 31;
  g = (v >> 5) & 63;
  b = v & 31;
  r = (r << 3) | (r >> 2);
  g = (g << 2) | (g >> 4);
  b = (b << 3) | (b >> 2);
}

/* Source pixel lookup in 16.16 fixed point: centre-aligned bilinear. */
static void axis(int dst, int src, int i, int &i0, int &i1, int &f) {
  int32_t p = (int32_t)((((int64_t)(2 * i + 1) * src << 16) / (2 * dst)) - (1 << 15));
  if (p < 0) p = 0;
  i0 = p >> 16;
  f = (p >> 8) & 0xFF; /* 0..255 weight of i1 */
  i1 = i0 + 1 < src ? i0 + 1 : i0;
}

/* per-column lookup, in PSRAM: internal SRAM is reserved for the panel */
static int *s_x0 = nullptr;

static void upscale(uint16_t *dst) {
  const uint16_t *src = (const uint16_t *)s_st;
  int *x0s = s_x0, *x1s = s_x0 + BACKDROP_W, *fxs = s_x0 + 2 * BACKDROP_W;
  for (int x = 0; x < BACKDROP_W; x++) axis(BACKDROP_W, s_sw, x, x0s[x], x1s[x], fxs[x]);
  for (int y = 0; y < BACKDROP_H; y++) {
    int y0, y1, fy;
    axis(BACKDROP_H, s_sh, y, y0, y1, fy);
    const uint16_t *r0 = src + y0 * s_sw, *r1 = src + y1 * s_sw;
    uint16_t *out = dst + y * BACKDROP_W;
    for (int x = 0; x < BACKDROP_W; x++) {
      int ar, ag, ab, br, bg, bb, cr, cg, cb, dr, dg, db;
      unpack(r0[x0s[x]], ar, ag, ab);
      unpack(r0[x1s[x]], br, bg, bb);
      unpack(r1[x0s[x]], cr, cg, cb);
      unpack(r1[x1s[x]], dr, dg, db);
      int fx = fxs[x];
      int tr = ar * (256 - fx) + br * fx, tg = ag * (256 - fx) + bg * fx, tb = ab * (256 - fx) + bb * fx;
      int ur = cr * (256 - fx) + dr * fx, ug = cg * (256 - fx) + dg * fx, ub = cb * (256 - fx) + db * fx;
      int r = (tr * (256 - fy) + ur * fy) >> 16;
      int g = (tg * (256 - fy) + ug * fy) >> 16;
      int b = (tb * (256 - fy) + ub * fy) >> 16;
      int d = BAYER[y & 3][x & 3];
      r += d >> 1; /* 5-bit channels lose 3 bits: dither in 0..7 */
      g += d >> 2; /* 6-bit: 0..3 */
      b += d >> 1;
      if (r > 255) r = 255;
      if (g > 255) g = 255;
      if (b > 255) b = 255;
      out[x] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
    }
  }
}

bool backdrop_upload_commit() {
  if (!s_st || s_len != s_need) return false;
  if (!s_x0) s_x0 = (int *)heap_caps_malloc(3 * BACKDROP_W * sizeof(int), MALLOC_CAP_SPIRAM);
  if (!s_x0) return false;
  int next = s_cur == 0 ? 1 : 0;
  if (!s_buf[next]) s_buf[next] = (uint8_t *)heap_caps_malloc(BACKDROP_W * BACKDROP_H * 2, MALLOC_CAP_SPIRAM);
  if (!s_buf[next]) return false;
  upscale((uint16_t *)s_buf[next]);
  s_dsc.header.magic = LV_IMAGE_HEADER_MAGIC;
  s_dsc.header.cf = LV_COLOR_FORMAT_RGB565;
  s_dsc.header.w = BACKDROP_W;
  s_dsc.header.h = BACKDROP_H;
  s_dsc.header.stride = BACKDROP_W * 2;
  s_dsc.data_size = BACKDROP_W * BACKDROP_H * 2;
  s_dsc.data = s_buf[next];
  memcpy(s_tag, s_st_tag, BACKDROP_TAG_LEN);
  s_cur = next;
  return true;
}

const lv_image_dsc_t *backdrop_get() { return s_cur >= 0 ? &s_dsc : nullptr; }
const char *backdrop_tag() { return s_cur >= 0 ? s_tag : ""; }
