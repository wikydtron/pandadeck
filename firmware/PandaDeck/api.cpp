#include "api.h"

#include "backdrop.h"
#include "display.h"
#include "hid.h"
#include "icon_store.h"
#include "net.h"
#include "pdlog.h"
#include "settings.h"
#include "store.h"
#include "usb_link.h"

#include <Arduino.h>
#include <Update.h>
#include <WiFi.h>
#include <esp32-hal-tinyusb.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <mbedtls/md.h>
#include <stdarg.h>
#include <sys/time.h>

static_assert(PROTO_IMG_KEY_LEN == ICON_KEY_LEN, "icon key length");
static_assert(PROTO_IMG_TAG_LEN == ICON_TAG_LEN && PROTO_IMG_TAG_LEN == BACKDROP_TAG_LEN, "image tag length");

#define API_SYNC_MAX (48 * 1024)
#define STAGING_STALE_MS 10000

extern volatile uint32_t g_loop_max_ms, g_loop_avg_ms, g_loop_stack_free;

/* ---- small JSON writer ------------------------------------------------------- */
struct Out {
  char *buf;
  size_t cap, n;
  bool full;
  void add(const char *fmt, ...) __attribute__((format(printf, 2, 3))) {
    if (full) return;
    va_list ap;
    va_start(ap, fmt);
    int r = vsnprintf(buf + n, cap - n, fmt, ap);
    va_end(ap);
    if (r < 0 || (size_t)r >= cap - n) {
      full = true;
      buf[n] = '\0';
      return;
    }
    n += (size_t)r;
  }
  void str(const char *s) { /* JSON string incl. quotes */
    char esc[200];
    proto_json_escape(esc, sizeof esc, s);
    add("\"%s\"", esc);
  }
  ApiResult done(int status) { return {full ? 500 : status, full ? 0 : n}; }
};

static Out out_of(char *buf, size_t cap) {
  Out o{buf, cap, 0, false};
  if (cap) buf[0] = '\0';
  return o;
}

static const char *b(bool v) { return v ? "true" : "false"; }

ApiResult api_error(char *buf, size_t cap, int status, const char *err) {
  Out o = out_of(buf, cap);
  o.add("{\"ok\":false,\"error\":");
  o.str(err ? err : "error");
  o.add("}");
  return o.done(status);
}

/* ---- deferred actions ---------------------------------------------------------- */
enum Pending : uint8_t { ACT_NONE, ACT_WIFI, ACT_REBOOT, ACT_BOOTLOADER };
static volatile uint8_t s_pending = ACT_NONE;

void api_after_reply() {
  uint8_t act = s_pending;
  s_pending = ACT_NONE;
  switch (act) {
    case ACT_WIFI:
      delay(200); /* an HTTP reply must leave before the link drops */
      net_apply_wifi();
      break;
    case ACT_REBOOT:
      pd_log("[api] rebooting\n");
      delay(400); /* let the reply drain (USB / TCP) */
      ESP.restart();
      break;
    case ACT_BOOTLOADER:
      pd_log("[api] rebooting into the ROM download mode\n");
      delay(400);
      /* Sets RTC_CNTL_FORCE_DOWNLOAD_BOOT in a shutdown hook and hands the
       * USB PHY back to the USB-Serial/JTAG controller, so the ROM loader
       * enumerates as 303A:1001 and esptool can flash over the same cable. */
      usb_persist_restart(RESTART_BOOTLOADER);
      ESP.restart(); /* not reached unless the hook could not be installed */
      break;
    default:
      break;
  }
}

/* ---- info ---------------------------------------------------------------------- */
static void add_features(Out &o) {
  o.add("\"features\":[\"usb\",\"wifi\"");
  o.add("]");
}

static void add_settings(Out &o, const Settings &s) {
  char js[400];
  proto_settings_json(s, js, sizeof js);
  o.add("\"settings\":%s", js[0] ? js : "{}");
}

static void add_wifi(Out &o, bool with_ssid) {
  char ip[16] = "", ssid[PD_SSID_LEN] = "";
  net_ip(ip, sizeof ip);
  o.add("\"wifi\":{\"configured\":%s,\"connected\":%s,\"source\":\"%s\",\"ip\":\"%s\",\"host\":\"%s.local\",",
        b(net_wifi_configured()), b(net_connected()), net_wifi_source(), ip, net_hostname());
  if (with_ssid) {
    net_ssid(ssid, sizeof ssid);
    o.add("\"ssid\":");
    o.str(ssid);
    o.add(",");
  }
  o.add("\"rssi\":%d}", net_connected() ? net_rssi() : 0);
}

static void add_partitions(Out &o) {
  o.add("\"partitions\":[");
  esp_partition_iterator_t it = esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, nullptr);
  bool first = true;
  while (it) {
    const esp_partition_t *p = esp_partition_get(it);
    o.add("%s{\"label\":\"%s\",\"type\":%d,\"sub\":%d,\"addr\":%lu,\"size\":%lu}", first ? "" : ",", p->label, p->type,
          p->subtype, (unsigned long)p->address, (unsigned long)p->size);
    first = false;
    it = esp_partition_next(it);
  }
  esp_partition_iterator_release(it);
  o.add("]");
}

static void add_diagnostics(Out &o) {
  const esp_partition_t *run = esp_ota_get_running_partition();
  const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
  esp_ota_img_states_t st = ESP_OTA_IMG_UNDEFINED;
  if (run) esp_ota_get_state_partition(run, &st);
  DisplayStats ds = display_stats();
  uint32_t since = store_ms_since_sync();
  o.add("\"heap_min\":%u,\"reset\":%d,\"ip\":\"%s\",\"rssi\":%d,\"mac\":\"%s\",\"running\":\"%s\",\"run_addr\":%lu,"
        "\"run_size\":%lu,\"next\":\"%s\",\"ota_state\":%d,\"valid_marked\":%s,\"frames\":%lu,",
        (unsigned)ESP.getMinFreeHeap(), (int)esp_reset_reason(), WiFi.localIP().toString().c_str(), WiFi.RSSI(),
        WiFi.macAddress().c_str(),
        run ? run->label : "?", run ? (unsigned long)run->address : 0ul, run ? (unsigned long)run->size : 0ul,
        next ? next->label : "?", (int)st, b(net_marked_valid()), (unsigned long)ds.frames);
  o.add("\"touches\":%lu,\"touch_ok\":%s,\"touch_res\":\"%ux%u\",\"bl\":%u,\"idle_ms\":%lu,\"since_sync_ms\":%ld,"
        "\"time_ok\":%s,\"fb_done\":%lu,\"bl_now\":%u,\"bl_override\":%s,\"bl_mode\":%u,\"raw_touch\":[%d,%d],",
        (unsigned long)ds.touches, b(ds.touch_ok), ds.touch_w, ds.touch_h, display_brightness(),
        (unsigned long)display_idle_ms(), since == UINT32_MAX ? -1L : (long)since, b(net_time_ok()),
        (unsigned long)ds.fb_done, ds.bl_now, b(ds.bl_override), ds.bl_mode, ds.raw_x, ds.raw_y);
  o.add("\"pclk\":%lu,\"frozen\":%s,\"bounce_lines\":%u,\"pclk_neg\":%s,\"loop_max_ms\":%lu,\"loop_avg_ms\":%lu,"
        "\"loop_stack_free\":%lu,\"icons\":%d,\"flip\":%s,\"tflip\":%s,",
        (unsigned long)display_pclk(), b(g_ui_frozen), display_cfg().bounce_lines, b(display_cfg().pclk_neg),
        (unsigned long)g_loop_max_ms, (unsigned long)g_loop_avg_ms, (unsigned long)g_loop_stack_free,
        icon_store_count(), b(display_flipped()), b(display_touch_flipped()));
  UsbLinkStats u = usb_link_stats();
  /* the USB receive path runs in Arduino's USB event task (2 KB stack) */
  TaskHandle_t evt = xTaskGetHandle("arduino_usb_eve");
  o.add("\"link\":{\"frames\":%lu,\"dropped\":%lu,\"overflows\":%lu,\"host\":%s,\"stack_free\":%lu,"
        "\"rx_stack_free\":%ld},",
        (unsigned long)u.frames, (unsigned long)u.dropped, (unsigned long)u.overflows, b(u.host_open),
        (unsigned long)net_link_stack_free(), evt ? (long)uxTaskGetStackHighWaterMark(evt) : -1L);
}

static bool hex_nonce(const char *s) {
  size_t n = s ? strlen(s) : 0;
  if (n < 8 || n > 128) return false;
  for (; *s; s++)
    if (!((*s >= '0' && *s <= '9') || (*s >= 'a' && *s <= 'f') || (*s >= 'A' && *s <= 'F'))) return false;
  return true;
}

/* proof = hex(HMAC-SHA256(token, nonce)): shows the token was known
 * without revealing it. */
static bool add_proof(Out &o, const char *nonce) {
  const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  const char *key = settings_token();
  uint8_t mac[32];
  if (!md || mbedtls_md_hmac(md, (const unsigned char *)key, strlen(key), (const unsigned char *)nonce, strlen(nonce),
                             mac) != 0)
    return false;
  o.add(",\"proof\":\"");
  for (uint8_t v : mac) o.add("%02x", v);
  o.add("\"");
  return true;
}

ApiResult api_info(char *buf, size_t cap, ApiOwner via, bool authed, const char *nonce) {
  Settings s = settings_get();
  Out o = out_of(buf, cap);
  o.add("{\"app\":\"pandadeck\",\"fw\":\"%s\",\"built\":\"%s %s\",\"boot\":%lu,\"board\":\"panda-touch\",\"name\":",
        FW_VERSION, __DATE__, __TIME__, (unsigned long)store_boot_id());
  o.str(s.name);
  o.add(",\"uptime_s\":%lu,\"heap\":%u,\"heap_internal\":%u,\"psram\":%u,\"usb\":%s,",
        (unsigned long)(millis() / 1000), (unsigned)ESP.getFreeHeap(),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT), (unsigned)ESP.getFreePsram(),
        b(hid_ready()));
  add_wifi(o, via == API_USB || authed);
  o.add(",");
  add_settings(o, s);
  o.add(",");
  add_features(o);
  if (hex_nonce(nonce)) add_proof(o, nonce);
  if (via == API_USB) {
    o.add(",\"token\":");
    o.str(settings_token());
  } else {
    o.add(",");
    add_diagnostics(o);
    add_partitions(o);
  }
  o.add("}");
  return o.done(200);
}

/* ---- sync ---------------------------------------------------------------------- */
ApiResult api_sync(const char *json, size_t len, char *buf, size_t cap) {
  if (len > API_SYNC_MAX) return api_error(buf, cap, 413, "too big");
  int64_t epoch = 0;
  bool ok = len == 0 || store_apply_sync(json, len, &epoch);
  /* No NTP (USB only, or before Wi-Fi is up): take the host's clock. */
  if (ok && epoch > 1700000000 && !net_time_ok()) {
    struct timeval tv = {(time_t)epoch, 0};
    settimeofday(&tv, nullptr);
  }
  Out o = out_of(buf, cap);
  o.add("{\"ok\":%s,\"fw\":\"%s\",\"boot\":%lu,\"icons\":%d,\"events\":", b(ok), FW_VERSION,
        (unsigned long)store_boot_id(), icon_store_count());
  if (o.full || o.cap - o.n < 4) return o.done(500);
  o.n += store_drain_events(o.buf + o.n, o.cap - o.n - 2);
  o.add("}");
  return o.done(ok ? 200 : 400);
}

/* ---- config -------------------------------------------------------------------- */
ApiResult api_config(const char *json, size_t len, char *buf, size_t cap) {
  SettingsPatch p;
  const char *err = nullptr;
  if (!proto_parse_settings(json, len, &p, &err)) {
    if (strcmp(err, "unknown") != 0) return api_error(buf, cap, 400, err);
    Out e = out_of(buf, cap); /* the app reads this as "firmware too old" */
    e.add("{\"ok\":false,\"error\":\"unknown\",\"key\":");
    e.str(p.unknown);
    e.add("}");
    return e.done(400);
  }
  Out o = out_of(buf, cap);
  if (p.reset || p.bootloader) {
    if (api_update_running()) return api_error(buf, cap, 409, "update in progress");
    if (p.reset) settings_factory_reset();
    s_pending = p.reset ? ACT_REBOOT : ACT_BOOTLOADER;
    o.add("{\"ok\":true,\"reboot\":true}");
    return o.done(200);
  }
  settings_apply(p);
  if (p.has_name) net_set_name(p.name);
  if (p.has_wifi) s_pending = ACT_WIFI;
  Settings s = settings_get();
  o.add("{\"ok\":true,");
  add_settings(o, s);
  o.add(",");
  add_wifi(o, true);
  o.add(",\"reboot\":false}");
  return o.done(200);
}

/* ---- images --------------------------------------------------------------------
 * The USB side starts / writes images from its receive callback while HTTP
 * runs in the link task, so staging ownership is claimed atomically. */
static uint8_t s_img_owner = API_NONE;
static uint8_t s_img_kind = IMG_ICON;
static volatile uint32_t s_img_since = 0;

static bool img_claim(ApiOwner who) {
  uint8_t cur = __atomic_load_n(&s_img_owner, __ATOMIC_ACQUIRE);
  for (;;) {
    bool free_or_mine = cur == API_NONE || cur == who;
    bool stale = millis() - s_img_since > STAGING_STALE_MS; /* owner vanished mid-upload */
    if (!free_or_mine && !stale) return false;
    if (__atomic_compare_exchange_n(&s_img_owner, &cur, (uint8_t)who, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
      break;
  }
  s_img_since = millis();
  return true;
}

static bool img_owned_by(ApiOwner who) { return __atomic_load_n(&s_img_owner, __ATOMIC_ACQUIRE) == who; }

static void img_release(ApiOwner who) {
  uint8_t cur = (uint8_t)who;
  __atomic_compare_exchange_n(&s_img_owner, &cur, (uint8_t)API_NONE, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
}

const char *api_image_begin(ApiOwner who, const ImageHeader &h) {
  if (!img_claim(who)) return "busy";
  bool ok = h.kind == IMG_ICON ? icon_upload_begin(h.key, h.w, h.h, h.tag)
                               : backdrop_upload_begin(h.w, h.h, h.tag);
  if (!ok) {
    img_release(who);
    return "bad image";
  }
  s_img_kind = h.kind;
  return nullptr;
}

void api_image_write(ApiOwner who, const uint8_t *data, size_t n) {
  if (!img_owned_by(who)) return;
  if (s_img_kind == IMG_ICON) icon_upload_write(data, n);
  else backdrop_upload_write(data, n);
}

const char *api_image_commit(ApiOwner who) {
  if (!img_owned_by(who)) return "not started";
  const char *e = s_img_kind == IMG_ICON ? icon_upload_commit()
                                         : (backdrop_upload_commit() ? nullptr : "incomplete image");
  img_release(who);
  return e;
}

void api_image_abort(ApiOwner who) { img_release(who); }

/* ---- firmware update ----------------------------------------------------------- */
static ApiOwner s_upd_owner = API_NONE;
static bool s_upd_sized = false;
static uint32_t s_upd_bytes = 0; /* accepted (Update buffers the last partial sector) */

bool api_update_running() { return s_upd_owner != API_NONE && Update.isRunning(); }
uint32_t api_update_written() { return s_upd_bytes; }

const char *api_update_begin(ApiOwner who, uint32_t size, const char *md5) {
  if (api_update_running()) {
    if (s_upd_owner != who) return "busy";
    Update.abort(); /* the same host starting over */
  }
  s_upd_owner = API_NONE;
  if (!Update.begin(size ? size : UPDATE_SIZE_UNKNOWN, U_FLASH)) {
    pd_log("[ota] begin failed: %s\n", Update.errorString());
    return Update.errorString();
  }
  if (md5 && md5[0]) Update.setMD5(md5);
  s_upd_bytes = 0;
  s_upd_owner = who;
  s_upd_sized = size != 0;
  pd_log("[ota] start (%s, %lu bytes)\n", who == API_USB ? "usb" : "http", (unsigned long)size);
  return nullptr;
}

const char *api_update_write(ApiOwner who, const uint8_t *data, size_t n) {
  if (s_upd_owner != who || !Update.isRunning()) return "not started";
  if (Update.write((uint8_t *)data, n) != n) {
    pd_log("[ota] write failed: %s\n", Update.errorString());
    return Update.errorString();
  }
  s_upd_bytes += (uint32_t)n;
  return nullptr;
}

const char *api_update_end(ApiOwner who) {
  if (s_upd_owner != who) return "not started";
  s_upd_owner = API_NONE;
  if (!Update.end(!s_upd_sized)) {
    pd_log("[ota] end failed: %s\n", Update.errorString());
    return Update.errorString();
  }
  pd_log("[ota] done %lu bytes\n", (unsigned long)Update.progress());
  s_pending = ACT_REBOOT;
  return nullptr;
}

void api_update_abort(ApiOwner who) {
  if (s_upd_owner != who) return;
  if (Update.isRunning()) Update.abort();
  s_upd_owner = API_NONE;
}
