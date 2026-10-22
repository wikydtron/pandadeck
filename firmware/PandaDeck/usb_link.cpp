#include "usb_link.h"

#include "api.h"
#include "backdrop.h"
#include "icon_store.h"
#include "link_codec.h"
#include "pdlog.h"
#include "proto.h"

#include <Arduino.h>
#include <USB.h>
#include <esp_heap_caps.h>
#include <freertos/semphr.h>
#include <string.h>

#define BUF_CAP (64u * 1024u) /* JSON requests and `D` chunks; PSRAM */
#define REPLY_CAP 6144u
#define IMG_HDR_MAX 256u
#define RX_CHUNK 512u
#define FRAME_STALL_MS 1500u
#define REPLY_LOCK_MS 1000u
#define CB_PUMP_WAIT_MS 20u

static SemaphoreHandle_t s_pump_mu = nullptr; /* decoder + sink state */
static uint8_t s_rx[RX_CHUNK];                /* pump scratch, under s_pump_mu */
static uint8_t *s_buf = nullptr;
static char *s_reply = nullptr;
static volatile bool s_ready = false; /* a complete frame waits for the link task */
static uint32_t s_handled = 0;
static volatile uint32_t s_overflows = 0;
static bool s_seen = false;
static LinkDecoder s_dec(LINK_MAX_PAYLOAD);

/* ---- reply ------------------------------------------------------------------------- */
static void send_frame(char type, const char *payload, size_t n) {
  uint8_t hdr[LINK_HEADER_LEN];
  link_encode_header(hdr, type, (uint32_t)n);
  if (!pd_serial_lock(REPLY_LOCK_MS)) return;
  Serial.write(hdr, sizeof hdr);
  if (n) Serial.write((const uint8_t *)payload, n);
  pd_serial_unlock();
}

static void send_result(char type, ApiResult r) {
  if (r.len == 0) r = api_error(s_reply, REPLY_CAP, 500, "reply too big");
  send_frame(type, s_reply, r.len);
}

static void send_error(char type, const char *err) { send_result(type, api_error(s_reply, REPLY_CAP, 400, err)); }

static void send_written(char type) {
  int n = snprintf(s_reply, REPLY_CAP, "{\"ok\":true,\"written\":%lu}", (unsigned long)api_update_written());
  send_frame(type, s_reply, (size_t)n);
}

/* ---- frame sink (runs in the receive pump) ---------------------------------------- */
enum Mode : uint8_t { M_SKIP, M_BUFFER, M_IMAGE };

class Sink : public LinkSink {
 public:
  bool done = false;
  char type = 0;
  Mode mode = M_SKIP;
  const char *err = nullptr;
  uint32_t len = 0, got = 0;
  bool img_on = false;

  bool frame_begin(char t, uint32_t n) override {
    type = t;
    len = n;
    got = 0;
    err = nullptr;
    done = false;
    img_on = false;
    hdr_n_ = 0;
    hdr_done_ = false;
    switch (t) {
      case 'H': case 'S': case 'C': case 'U': case 'D': case 'P':
        if (n > BUF_CAP) return fail("too big");
        mode = M_BUFFER;
        return true;
      case 'I':
        mode = M_IMAGE;
        return true;
      default:
        return fail("unknown");
    }
  }

  void frame_data(const uint8_t *p, size_t n) override {
    if (mode == M_BUFFER) {
      memcpy(s_buf + got, p, n); /* frame_begin capped len at BUF_CAP */
      got += (uint32_t)n;
    } else if (mode == M_IMAGE) {
      image_data(p, n);
    }
  }

  void frame_end(char, bool) override { done = true; }

  void frame_abort(char) override {
    if (img_on) api_image_abort(API_USB);
    img_on = false;
    done = false;
  }

 private:
  char hdr_[IMG_HDR_MAX];
  uint32_t hdr_n_ = 0;
  bool hdr_done_ = false;

  bool fail(const char *e) {
    err = e;
    mode = M_SKIP;
    return false;
  }

  /* `I` payload: JSON header line, '\n', then pixels streamed straight into
   * the icon / backdrop staging buffer (no second copy of the image). */
  void image_data(const uint8_t *p, size_t n) {
    if (!hdr_done_) {
      const uint8_t *nl = (const uint8_t *)memchr(p, '\n', n);
      size_t k = nl ? (size_t)(nl - p) : n;
      if (hdr_n_ + k >= IMG_HDR_MAX) {
        fail("bad header");
        return;
      }
      memcpy(hdr_ + hdr_n_, p, k);
      hdr_n_ += (uint32_t)k;
      if (!nl) return;
      p += k + 1;
      n -= k + 1;
      hdr_done_ = true;
      if (!start_image()) return;
    }
    if (img_on && n) api_image_write(API_USB, p, n);
  }

  bool start_image() {
    ImageHeader h;
    const char *e = nullptr;
    if (!proto_parse_image_header(hdr_, hdr_n_, &h, &e)) return fail(e);
    if (proto_image_bytes(h) != len - hdr_n_ - 1) return fail("size mismatch");
    if ((e = api_image_begin(API_USB, h)) != nullptr) return fail(e);
    img_on = true;
    return true;
  }
};

static Sink s_sink;

/* ---- receive pump -------------------------------------------------------------------
 * Runs in the CDC RX event (Arduino's USB event task, high priority) and moves
 * bytes from the CDC queue straight through the decoder: payloads land in
 * s_buf / the image staging buffer as they arrive. Arduino's CDC drops bytes
 * if its 256-byte queue isn't drained within ~10 ms, so this must never wait
 * on HTTP or the UI. It reads exactly what the current frame still needs and
 * stops at a finished frame until the link task has answered it. `budget`
 * caps the bytes taken (the link task only collects what queued up while it
 * was answering; the RX events carry the rest at TinyUSB's priority). */
static void pump(TickType_t wait, size_t budget) {
  if (!s_buf || xSemaphoreTake(s_pump_mu, wait) != pdTRUE) return;
  while (budget && !__atomic_load_n(&s_ready, __ATOMIC_ACQUIRE)) {
    int avail = Serial.available();
    if (avail <= 0) break;
    size_t n = s_dec.need();
    if (n > budget) n = budget;
    if (n > (size_t)avail) n = (size_t)avail;
    if (n > sizeof s_rx) n = sizeof s_rx;
    n = Serial.read(s_rx, n);
    if (!n) break;
    budget -= n;
    s_dec.feed(s_rx, n, s_sink, millis());
    if (s_sink.done) __atomic_store_n(&s_ready, true, __ATOMIC_RELEASE);
  }
  xSemaphoreGive(s_pump_mu);
}

/* Decoding right in the CDC RX event keeps the reader at TinyUSB's own
 * priority: a lower-priority reader gets preempted by TinyUSB for every byte
 * it frees in the 256-byte CDC queue (one context switch per byte), which
 * starved the idle task into the task watchdog. */
static void on_cdc_rx(void *, esp_event_base_t, int32_t, void *) { pump(pdMS_TO_TICKS(CB_PUMP_WAIT_MS), SIZE_MAX); }
static void on_cdc_overflow(void *, esp_event_base_t, int32_t, void *) { s_overflows = s_overflows + 1; }

/* ---- request handlers (link task) ------------------------------------------------------ */
static void handle_update(char rt, const Sink &k) {
  bool is_json = k.type == 'U' && k.got > 0 && s_buf[0] == '{';
  if (!is_json) { /* `D` chunk (or a raw `U` chunk) */
    const char *e = api_update_write(API_USB, s_buf, k.got);
    if (e) send_error(rt, e);
    else send_written(rt);
    return;
  }
  UpdateCmd cmd;
  const char *e = nullptr;
  if (!proto_parse_update((const char *)s_buf, k.got, &cmd, &e)) {
    send_error(rt, e);
    return;
  }
  if (cmd.kind == UPD_BEGIN) e = api_update_begin(API_USB, cmd.size, cmd.md5);
  else if (cmd.kind == UPD_END) e = api_update_end(API_USB);
  else api_update_abort(API_USB);
  if (e) {
    send_error(rt, e);
    return;
  }
  send_written(rt);
  api_after_reply(); /* reboots after a verified end */
}

static void handle_frame(const Sink &k) {
  char rt = link_reply_type(k.type);
  if (k.err) {
    if (k.img_on) api_image_abort(API_USB);
    send_error(rt, k.err);
    return;
  }
  s_handled++;
  s_seen = true;
  switch (k.type) {
    case 'P':
      send_frame(rt, nullptr, 0);
      break;
    case 'H':
      send_result(rt, api_info(s_reply, REPLY_CAP, API_USB, true));
      break;
    case 'S':
      send_result(rt, api_sync((const char *)s_buf, k.got, s_reply, REPLY_CAP));
      break;
    case 'C':
      send_result(rt, api_config((const char *)s_buf, k.got, s_reply, REPLY_CAP));
      api_after_reply();
      break;
    case 'I': {
      const char *e = k.img_on ? api_image_commit(API_USB) : "incomplete image";
      if (e) send_error(rt, e);
      else send_frame(rt, "{\"ok\":true}", 11);
      break;
    }
    case 'U':
    case 'D':
      handle_update(rt, k);
      break;
    default:
      send_error(rt, "unknown");
      break;
  }
}

/* ---- public -------------------------------------------------------------------------- */
void usb_link_init() {
  s_buf = (uint8_t *)heap_caps_malloc(BUF_CAP + 1, MALLOC_CAP_SPIRAM);
  s_reply = (char *)heap_caps_malloc(REPLY_CAP, MALLOC_CAP_SPIRAM);
  s_pump_mu = xSemaphoreCreateMutex();
  /* staging is begun from the RX callback (2 KB stack): allocate it here */
  if (!s_buf || !s_reply || !s_pump_mu || !icon_upload_reserve() || !backdrop_upload_reserve()) {
    pd_log("[usb] link disabled: out of memory\n");
    s_buf = nullptr;
    return;
  }
  Serial.onEvent(ARDUINO_USB_CDC_RX_EVENT, on_cdc_rx);
  Serial.onEvent(ARDUINO_USB_CDC_RX_OVERFLOW_EVENT, on_cdc_overflow);
}

bool usb_link_poll() {
  if (!s_buf) return false;
  if (__atomic_load_n(&s_ready, __ATOMIC_ACQUIRE)) {
    s_sink.done = false;
    handle_frame(s_sink);
    __atomic_store_n(&s_ready, false, __ATOMIC_RELEASE);
    int queued = Serial.available(); /* arrived while we were busy: no RX event will repeat it */
    if (queued > 0) pump(portMAX_DELAY, (size_t)queued);
    return true;
  }
  if (xSemaphoreTake(s_pump_mu, 0) == pdTRUE) {
    s_dec.check_timeout(millis(), FRAME_STALL_MS, s_sink);
    xSemaphoreGive(s_pump_mu);
  }
  return false;
}

bool usb_link_seen() { return s_seen; }

UsbLinkStats usb_link_stats() { return {s_handled, s_dec.dropped(), s_overflows, (bool)Serial}; }
