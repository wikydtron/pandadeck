// Host tests for firmware/PandaDeck/icon_store.cpp (ESP-IDF, FreeRTOS and LVGL
// are stubbed in tests/stubs). Build + run: tests/run_host_tests.sh
#include <cstdio>
#include <cstring>
#include <vector>

#include "esp_heap_caps.h"
#include "icon_store.h"

static int g_fail = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    if (!(cond)) {                                                      \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);       \
      g_fail++;                                                         \
    }                                                                   \
  } while (0)

static const size_t STAGING = (size_t)ICON_MAX_DIM * ICON_MAX_DIM * 3;

/* Upload a w x h image under `key` whose bytes are all `fill`; returns the
 * commit error (nullptr = published). `send` < w*h*3 sends a truncated image. */
static const char *upload(const char *key, uint16_t w, uint16_t h, uint8_t fill, long send = -1) {
  if (!icon_upload_begin(key, w, h, "t")) return "begin refused";
  std::vector<uint8_t> px(send < 0 ? (size_t)w * h * 3 : (size_t)send, fill);
  for (size_t at = 0; at < px.size(); at += 4096)
    icon_upload_write(px.data() + at, px.size() - at < 4096 ? px.size() - at : 4096);
  return icon_upload_commit();
}

/* The published image never claims more bytes than its buffer holds. */
static bool fits(const char *key) {
  const lv_image_dsc_t *d = icon_store_get(key);
  return d && stub_heap::size_of(d->data) >= d->data_size;
}

static void test_publishes_an_image() {
  CHECK(upload("app0", 80, 80, 7) == nullptr);
  const lv_image_dsc_t *d = icon_store_get("app0");
  CHECK(d && d->header.w == 80 && d->header.h == 80 && d->data_size == 80u * 80 * 3);
  CHECK(d && d->data[0] == 7 && d->data[d->data_size - 1] == 7);
  CHECK(!std::strcmp(icon_store_tag("app0"), "t"));
}

/* Classic -> Icons style makes every deck icon bigger. The second upload at the
 * new size lands in the other ping-pong buffer, which must have grown too. */
static void test_growing_icon_never_overruns_its_buffer() {
  CHECK(upload("grow", 80, 80, 1) == nullptr);
  CHECK(upload("grow", 80, 80, 2) == nullptr);
  for (uint8_t i = 3; i < 7; i++) {
    CHECK(upload("grow", 96, 96, i) == nullptr);
    CHECK(fits("grow"));
    const lv_image_dsc_t *d = icon_store_get("grow");
    CHECK(d && d->header.w == 96 && d->data[d->data_size - 1] == i);
  }
}

/* Switching styles back and forth must not leak: each key keeps at most its
 * two buffers, sized for the largest image it has had. */
static void test_switching_sizes_does_not_leak() {
  upload("swap", 56, 56, 1);
  size_t before = stub_heap::live_bytes;
  for (int i = 0; i < 40; i++) {
    uint16_t px = (i % 3 == 0) ? 56 : (i % 3 == 1) ? 80 : 96;
    CHECK(upload("swap", px, px, (uint8_t)i) == nullptr);
    CHECK(fits("swap"));
  }
  size_t grown = stub_heap::live_bytes - before;
  CHECK(grown <= 2u * 96 * 96 * 3);
  if (grown > 2u * 96 * 96 * 3) std::printf("  leaked %zu bytes over 40 uploads\n", grown);
}

static void test_shrinking_reuses_the_bigger_buffer() {
  CHECK(upload("shrink", 96, 96, 1) == nullptr);
  CHECK(upload("shrink", 96, 96, 2) == nullptr);
  size_t before = stub_heap::live_bytes;
  CHECK(upload("shrink", 56, 56, 3) == nullptr);
  CHECK(upload("shrink", 56, 56, 4) == nullptr);
  CHECK(stub_heap::live_bytes == before);
  const lv_image_dsc_t *d = icon_store_get("shrink");
  CHECK(d && d->header.w == 56 && d->data_size == 56u * 56 * 3 && d->header.stride == 56 * 2);
}

static void test_truncated_upload_is_refused_and_keeps_the_old_image() {
  CHECK(upload("trunc", 64, 64, 9) == nullptr);
  const char *e = upload("trunc", 64, 64, 1, 64 * 64 * 3 - 10);
  CHECK(e && !std::strcmp(e, "incomplete image"));
  const lv_image_dsc_t *d = icon_store_get("trunc");
  CHECK(d && d->data[0] == 9);
}

/* PSRAM exhausted: say so (not "incomplete image"), keep showing the old one. */
static void test_out_of_memory_is_reported_as_such() {
  CHECK(upload("oom", 64, 64, 5) == nullptr);
  stub_heap::fail_allocs = 1;
  const char *e = upload("oom", 96, 96, 6);
  stub_heap::fail_allocs = 0;
  CHECK(e && !std::strcmp(e, "out of memory"));
  const lv_image_dsc_t *d = icon_store_get("oom");
  CHECK(d && d->header.w == 64 && d->data[0] == 5 && fits("oom"));
  CHECK(upload("oom", 96, 96, 7) == nullptr); /* recovers once memory is back */
  CHECK(fits("oom"));
}

static void test_new_key_without_memory_is_not_published() {
  stub_heap::fail_allocs = 1;
  const char *e = upload("nomem", 32, 32, 1);
  stub_heap::fail_allocs = 0;
  CHECK(e && !std::strcmp(e, "out of memory"));
  CHECK(icon_store_get("nomem") == nullptr);
}

int main() {
  CHECK(icon_upload_reserve());
  CHECK(stub_heap::live_bytes == STAGING);
  test_publishes_an_image();
  test_growing_icon_never_overruns_its_buffer();
  test_switching_sizes_does_not_leak();
  test_shrinking_reuses_the_bigger_buffer();
  test_truncated_upload_is_refused_and_keeps_the_old_image();
  test_out_of_memory_is_reported_as_such();
  test_new_key_without_memory_is_not_published();
  std::printf(g_fail ? "icon_store: %d FAILED\n" : "icon_store: all passed\n", g_fail);
  return g_fail ? 1 : 0;
}
