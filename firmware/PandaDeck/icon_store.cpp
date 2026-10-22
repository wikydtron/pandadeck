#include "icon_store.h"

#include <Arduino.h>
#include <esp_heap_caps.h>
#include <freertos/semphr.h>
#include <string.h>

/* Each image has two PSRAM buffers used ping-pong: an update is written into
 * the one not on screen and then the descriptor is pointed at it, so the UI
 * never draws a half-copied image (the cover changes every track). */
struct Icon {
  char key[ICON_KEY_LEN];
  char tag[ICON_TAG_LEN];
  lv_image_dsc_t dsc;
  uint8_t *buf[2]; /* PSRAM, at least w*h*3 bytes each */
  size_t cap[2];   /* bytes allocated for buf[i] */
  uint8_t cur;     /* buffer the descriptor points at */
};

static Icon s_icons[ICON_MAX];
static volatile int s_count = 0;
static volatile uint32_t s_rev = 0;
static SemaphoreHandle_t s_mu = nullptr;

/* staging (link task only, one upload at a time: api.cpp) */
static char s_st_key[ICON_KEY_LEN];
static char s_st_tag[ICON_TAG_LEN];
static uint16_t s_st_w = 0, s_st_h = 0;
static uint8_t *s_st_buf = nullptr;
static size_t s_st_len = 0, s_st_need = 0;

static Icon *find(const char *key) {
  for (int i = 0; i < s_count; i++)
    if (!strncmp(s_icons[i].key, key, ICON_KEY_LEN)) return &s_icons[i];
  return nullptr;
}

bool icon_upload_reserve() {
  if (!s_mu) s_mu = xSemaphoreCreateMutex();
  if (!s_st_buf) s_st_buf = (uint8_t *)heap_caps_malloc((size_t)ICON_MAX_DIM * ICON_MAX_DIM * 3, MALLOC_CAP_SPIRAM);
  return s_mu && s_st_buf;
}

bool icon_upload_begin(const char *key, uint16_t w, uint16_t h, const char *tag) {
  if (!s_mu) s_mu = xSemaphoreCreateMutex();
  if (!key || !*key || strlen(key) >= ICON_KEY_LEN) return false;
  if (w == 0 || h == 0 || w > ICON_MAX_DIM || h > ICON_MAX_DIM) return false;
  strncpy(s_st_key, key, ICON_KEY_LEN);
  strncpy(s_st_tag, tag ? tag : "", ICON_TAG_LEN - 1);
  s_st_tag[ICON_TAG_LEN - 1] = '\0';
  s_st_w = w;
  s_st_h = h;
  s_st_need = (size_t)w * h * 3;
  s_st_len = 0;
  if (!s_st_buf) s_st_buf = (uint8_t *)heap_caps_malloc((size_t)ICON_MAX_DIM * ICON_MAX_DIM * 3, MALLOC_CAP_SPIRAM);
  return s_st_buf != nullptr;
}

void icon_upload_write(const uint8_t *data, size_t len) {
  if (!s_st_buf || s_st_len >= s_st_need) return;
  size_t n = len < s_st_need - s_st_len ? len : s_st_need - s_st_len;
  memcpy(s_st_buf + s_st_len, data, n);
  s_st_len += n;
}

/* Buffer `i` of `ic`, big enough for the staged image. Buffer `i` is never the
 * one on screen (the descriptor points at the other), so a too-small one is
 * freed and replaced; a bigger one is kept (icons shrink and grow back when
 * the deck style changes). */
static uint8_t *slot(Icon *ic, int i) {
  if (ic->buf[i] && ic->cap[i] >= s_st_need) return ic->buf[i];
  heap_caps_free(ic->buf[i]);
  ic->buf[i] = (uint8_t *)heap_caps_malloc(s_st_need, MALLOC_CAP_SPIRAM);
  ic->cap[i] = ic->buf[i] ? s_st_need : 0;
  return ic->buf[i];
}

const char *icon_upload_commit() {
  if (!s_st_buf || s_st_len != s_st_need) return "incomplete image";
  xSemaphoreTake(s_mu, portMAX_DELAY);
  Icon *ic = find(s_st_key);
  bool fresh = false;
  if (!ic && s_count < ICON_MAX) {
    ic = &s_icons[s_count];
    memset(ic, 0, sizeof *ic);
    strncpy(ic->key, s_st_key, ICON_KEY_LEN);
    fresh = true;
  }
  const char *err = ic ? nullptr : "too many images";
  if (ic) {
    int next = fresh ? 0 : 1 - ic->cur;
    uint8_t *dst = slot(ic, next);
    if (!dst) {
      err = "out of memory";
    } else {
      memcpy(dst, s_st_buf, s_st_need);
      ic->dsc.header.magic = LV_IMAGE_HEADER_MAGIC;
      ic->dsc.header.cf = LV_COLOR_FORMAT_RGB565A8;
      ic->dsc.header.w = s_st_w;
      ic->dsc.header.h = s_st_h;
      ic->dsc.header.stride = s_st_w * 2;
      ic->dsc.data_size = s_st_need;
      ic->dsc.data = dst;
      ic->cur = (uint8_t)next;
      memcpy(ic->tag, s_st_tag, ICON_TAG_LEN);
      if (fresh) s_count++; /* publish only once complete */
      s_rev++;
    }
  }
  xSemaphoreGive(s_mu);
  return err;
}

const lv_image_dsc_t *icon_store_get(const char *key) {
  if (!key || !*key) return nullptr;
  Icon *ic = find(key);
  return ic && ic->dsc.data ? &ic->dsc : nullptr;
}

const char *icon_store_tag(const char *key) {
  if (!key || !*key) return "";
  Icon *ic = find(key);
  return ic && ic->dsc.data ? ic->tag : "";
}

uint32_t icon_store_rev() { return s_rev; }
int icon_store_count() { return s_count; }
