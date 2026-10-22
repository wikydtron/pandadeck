#include "net.h"

#include "api.h"
#include "board_panda.h"
#include "display.h"
#include "features.h"
#include "pdlog.h"
#include "settings.h"
#include "usb_link.h"

#include <Arduino.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_core_dump.h>
#include <esp_heap_caps.h>
#include <esp_ota_ops.h>
#include <esp_wifi.h>
#include <time.h>

#if defined(WIFI_SSID) && !defined(WIFI_PASS)
#define WIFI_PASS ""
#endif

#define REPLY_CAP 6144
#define LINK_TASK_STACK 8192

/* WebServer keeps multipart form fields private until the whole body is
 * parsed; the firmware upload needs the `token` field that precedes the file
 * part while the file is still streaming in. */
class PdWebServer : public WebServer {
 public:
  using WebServer::WebServer;
  String postArg(const char *name) const {
    for (int i = 0; i < _postArgsLen; i++)
      if (_postArgs[i].key == name) return _postArgs[i].value;
    return String();
  }
};

static PdWebServer s_srv(80);
static char *s_reply = nullptr; /* PSRAM, link task only */
static bool s_http_started = false;
static bool s_marked_valid = false;
static bool s_mdns_up = false;
static const char *s_wifi_src = "none";
static TaskHandle_t s_task = nullptr;

static const char *HDR_KEYS[] = {"X-Panda-Token", "X-Panda-Nonce"};

/* ---- auth ---------------------------------------------------------------- */
/* Constant-time compare: don't leak the token one byte at a time. */
static bool token_ok(const String &t) {
  const char *want = settings_token();
  size_t n = strlen(want);
  if (n == 0 || t.length() != n) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < n; i++) diff |= (uint8_t)(t[i] ^ want[i]);
  return diff == 0;
}

/* JSON API: header only. */
static bool authed() { return token_ok(s_srv.header("X-Panda-Token")); }

/* Firmware upload: a browser can't set headers, so also ?token= or a
 * multipart field named `token` sent before the file. */
static bool authed_update() { return authed() || token_ok(s_srv.arg("token")) || token_ok(s_srv.postArg("token")); }

static void send_json(ApiResult r) {
  if (r.len == 0) r = api_error(s_reply, REPLY_CAP, 500, "reply too big");
  s_srv.send_P(r.status, "application/json", s_reply, r.len);
}

static bool require_token() {
  if (authed()) return true;
  s_srv.send(401, "application/json", "{\"ok\":false,\"error\":\"token\"}");
  return false;
}

/* ---- pages ----------------------------------------------------------------- */
#define PAGE_HEAD                                                                                          \
  "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,"   \
  "initial-scale=1'><title>PandaDeck</title><style>body{font:15px system-ui,sans-serif;background:"    \
  "#141413;color:#F0EEE6;max-width:560px;margin:40px auto;padding:0 16px}a,code{color:#D97757}"         \
  "label{display:block;margin:12px 0 4px;color:#A8A69E}input,button{font:inherit;padding:9px;width:"    \
  "100%;box-sizing:border-box;border-radius:6px;border:1px solid #37352F;background:#1E1D1B;color:"     \
  "inherit}button{margin-top:16px;background:#D97757;color:#141413;border:0;cursor:pointer}button:"     \
  "disabled{opacity:.5}progress{width:100%;margin-top:16px}#m{min-height:1.4em;color:#A8A69E}</style>"

static void handle_root() {
  static const char PAGE[] = PAGE_HEAD
      "<h1>PandaDeck</h1><p>Firmware <code>" FW_VERSION "</code> &middot; built " __DATE__ " " __TIME__
      "</p><p><a href='/update'>Update firmware</a> &middot; <a href='/api/info'>/api/info</a></p>";
  s_srv.send_P(200, "text/html", PAGE, sizeof PAGE - 1);
}

static void handle_update_page() {
  static const char PAGE[] = PAGE_HEAD
      "<h1>Update firmware</h1><p>Running <code>" FW_VERSION "</code>. The token is shown in the "
      "PandaDeck app's panel settings.</p>"
      "<form id=f method=POST action='/update' enctype='multipart/form-data'>"
      "<label for=t>Token</label><input id=t name=token type=password autocomplete=off required>"
      "<label for=b>Firmware file (.bin)</label><input id=b name=firmware type=file accept='.bin' required>"
      "<button id=u>Upload and restart</button></form><progress id=p max=100 value=0 hidden></progress>"
      "<p id=m></p><script>"
      "const f=document.getElementById('f'),u=document.getElementById('u'),p=document.getElementById('p'),"
      "m=document.getElementById('m');"
      "f.onsubmit=e=>{e.preventDefault();const x=new XMLHttpRequest();u.disabled=true;p.hidden=false;"
      "p.value=0;m.textContent='Uploading...';"
      "x.upload.onprogress=e=>{if(e.lengthComputable){p.value=100*e.loaded/e.total;"
      "if(e.loaded==e.total)m.textContent='Verifying...'}};"
      "x.onload=()=>{if(x.status==200){m.textContent='Done. The panel is restarting.'}else{"
      "m.textContent='Failed ('+x.status+'): '+x.responseText;u.disabled=false}};"
      "x.onerror=()=>{m.textContent='Connection lost.';u.disabled=false};"
      "x.open('POST','/update');x.send(new FormData(f))}</script>";
  s_srv.send_P(200, "text/html", PAGE, sizeof PAGE - 1);
}

/* ---- JSON API -------------------------------------------------------------- */
/* No token needed. X-Panda-Nonce gets an HMAC proof of the token back. */
static void handle_info() {
  String nonce = s_srv.header("X-Panda-Nonce");
  send_json(api_info(s_reply, REPLY_CAP, API_HTTP, authed(), nonce.length() ? nonce.c_str() : nullptr));
}

static void handle_sync() {
  if (!require_token()) return;
  const String &body = s_srv.arg("plain");
  send_json(api_sync(body.c_str(), body.length(), s_reply, REPLY_CAP));
}

static void handle_config() {
  if (!require_token()) return;
  const String &body = s_srv.arg("plain");
  send_json(api_config(body.c_str(), body.length(), s_reply, REPLY_CAP));
  api_after_reply();
}

/* ---- images (multipart: binary-safe, unlike the "plain" body String) ------- */
static bool s_img_auth = false, s_img_started = false, s_img_seen = false;
static const char *s_img_err = nullptr;

static void image_upload(uint8_t kind) {
  HTTPUpload &up = s_srv.upload();
  if (up.status == UPLOAD_FILE_START) {
    s_img_seen = true;
    s_img_started = false;
    s_img_err = nullptr;
    s_img_auth = authed();
    if (!s_img_auth) return;
    ImageHeader h = {};
    h.kind = kind;
    strncpy(h.key, s_srv.arg("key").c_str(), sizeof h.key - 1);
    strncpy(h.tag, s_srv.arg("tag").c_str(), sizeof h.tag - 1);
    h.w = (uint16_t)s_srv.arg("w").toInt();
    h.h = (uint16_t)s_srv.arg("h").toInt();
    s_img_err = api_image_begin(API_HTTP, h);
    s_img_started = s_img_err == nullptr;
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (s_img_started) api_image_write(API_HTTP, up.buf, up.currentSize);
  } else if (up.status == UPLOAD_FILE_END) {
    if (s_img_started) s_img_err = api_image_commit(API_HTTP);
    s_img_started = false;
  } else if (up.status == UPLOAD_FILE_ABORTED) {
    if (s_img_started) api_image_abort(API_HTTP);
    s_img_started = false;
    s_img_err = "aborted";
  }
}

static void image_done() {
  bool auth = s_img_auth, seen = s_img_seen;
  const char *err = s_img_err;
  if (s_img_started) api_image_abort(API_HTTP);
  s_img_auth = s_img_started = s_img_seen = false; /* never leak into the next request */
  s_img_err = nullptr;
  if (!auth && !authed()) {
    s_srv.send(401, "text/plain", "token");
    return;
  }
  if (!seen) err = "no file";
  if (err) s_srv.send(!strcmp(err, "busy") ? 503 : 400, "text/plain", err);
  else s_srv.send(200, "text/plain", "ok");
}

static void handle_icon_upload() { image_upload(IMG_ICON); }
static void handle_backdrop_upload() { image_upload(IMG_BACKDROP); }

/* ---- OTA ------------------------------------------------------------------- */
static bool s_upd_auth = false, s_upd_started = false;
static const char *s_upd_err = nullptr;

static void handle_update_upload() {
  HTTPUpload &up = s_srv.upload();
  if (up.status == UPLOAD_FILE_START) {
    s_upd_started = false;
    s_upd_err = nullptr;
    s_upd_auth = authed_update();
    if (!s_upd_auth) return;
    s_upd_err = api_update_begin(API_HTTP, 0, nullptr);
    s_upd_started = s_upd_err == nullptr;
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (s_upd_started && !s_upd_err) {
      s_upd_err = api_update_write(API_HTTP, up.buf, up.currentSize);
      if (s_upd_err) api_update_abort(API_HTTP);
    }
  } else if (up.status == UPLOAD_FILE_END) {
    if (s_upd_started && !s_upd_err) s_upd_err = api_update_end(API_HTTP);
  } else if (up.status == UPLOAD_FILE_ABORTED) {
    if (s_upd_started) api_update_abort(API_HTTP);
    s_upd_err = "aborted";
  }
}

static void handle_update_done() {
  bool auth = s_upd_auth, started = s_upd_started;
  const char *err = s_upd_err;
  s_upd_auth = s_upd_started = false; /* never leak into the next request */
  s_upd_err = nullptr;
  if (!auth) {
    s_srv.send(401, "text/plain", "bad token");
    return;
  }
  if (!started && !err) err = "no file";
  if (err) {
    api_update_abort(API_HTTP);
    s_srv.send(!strcmp(err, "busy") ? 503 : 500, "text/plain", String("update failed: ") + err);
    return;
  }
  s_srv.send(200, "text/plain", "OK - rebooting into new firmware");
  api_after_reply();
}

/* ---- debug / test (token in header) --------------------------------------- */
static void handle_screen() {
  /* The screen can show prompt text / commands: token required. */
  if (!require_token()) return;
  const uint8_t *fb = (const uint8_t *)display_snapshot(300);
  if (!fb) {
    s_srv.send(503, "text/plain", "no framebuffer");
    return;
  }
  const size_t total = PANEL_W * PANEL_H * 2;
  s_srv.sendHeader("X-Width", String(PANEL_W));
  s_srv.sendHeader("X-Height", String(PANEL_H));
  s_srv.sendHeader("X-Format", "RGB565LE");
  s_srv.sendHeader("X-Rotate", ((DISPLAY_ROTATE_180 != 0) != display_flipped()) ? "180" : "0"); /* scan order */
  s_srv.setContentLength(total);
  s_srv.send(200, "application/octet-stream", "");
  static uint8_t chunk[4096];
  for (size_t off = 0; off < total; off += sizeof chunk) {
    size_t n = total - off < sizeof chunk ? total - off : sizeof chunk;
    memcpy(chunk, fb + off, n);
    s_srv.sendContent((const char *)chunk, n);
  }
}

static void handle_tap() {
  if (!require_token()) return;
  int x = s_srv.arg("x").toInt(), y = s_srv.arg("y").toInt();
  int ms = s_srv.hasArg("ms") ? s_srv.arg("ms").toInt() : 120;
  display_inject_touch(x, y, ms);
  s_srv.send(200, "text/plain", "ok");
}

static void handle_debug_gpio() {
  if (!require_token()) return;
  int pin = s_srv.arg("pin").toInt(), level = s_srv.arg("level").toInt();
  bool ok = display_debug_gpio(pin, level);
  s_srv.send(ok ? 200 : 400, "text/plain", ok ? "ok" : "pin not allowed");
}

static void handle_debug_bl() {
  if (!require_token()) return;
  display_debug_bl(s_srv.arg("duty").toInt(), s_srv.arg("invert") == "1", s_srv.arg("release") == "1");
  s_srv.send(200, "text/plain", "ok");
}

static void handle_debug_pclk() {
  if (!require_token()) return;
  bool ok = display_debug_pclk((uint32_t)s_srv.arg("hz").toInt());
  s_srv.send(ok ? 200 : 400, "text/plain", ok ? "ok" : "bad hz");
}

/* Persist panel streaming config and reboot into it.
 * ?bb=<bounce lines 0-40>&pclk=<hz>&neg=<0|1>; omitted args keep current. */
static void handle_debug_dispcfg() {
  if (!require_token()) return;
  DisplayCfg c = display_cfg();
  if (s_srv.hasArg("bb")) c.bounce_lines = (uint8_t)s_srv.arg("bb").toInt();
  if (s_srv.hasArg("pclk")) c.pclk_hz = (uint32_t)s_srv.arg("pclk").toInt();
  if (s_srv.hasArg("neg")) c.pclk_neg = s_srv.arg("neg") == "1";
  if (!display_save_cfg(c)) {
    s_srv.send(400, "text/plain", "bad config");
    return;
  }
  s_srv.send(200, "text/plain", "saved - rebooting");
  delay(300);
  ESP.restart();
}

static void handle_debug_flip() {
  if (!require_token()) return;
  /* ?disp=0|1 picture, ?touch=0|1 touch; ?on= kept for both (old form). */
  if (s_srv.hasArg("on")) {
    bool on = s_srv.arg("on") == "1";
    display_request_flip(on);
    display_set_touch_flip(on);
  }
  if (s_srv.hasArg("disp")) display_request_flip(s_srv.arg("disp") == "1");
  if (s_srv.hasArg("touch")) display_set_touch_flip(s_srv.arg("touch") == "1");
  if (s_srv.hasArg("lcdreset")) display_debug_lcd_reset();
  s_srv.send(200, "text/plain", "ok");
}

static void handle_debug_freeze() {
  if (!require_token()) return;
  g_ui_frozen = s_srv.arg("on") == "1";
  s_srv.send(200, "text/plain", g_ui_frozen ? "frozen" : "running");
}

/* Summary of the last crash in the coredump partition (task, PC, backtrace)
 * for addr2line against the matching firmware.elf. */
static void handle_debug_coredump() {
  if (!require_token()) return;
  static esp_core_dump_summary_t sum;
  esp_err_t err = esp_core_dump_get_summary(&sum);
  if (err != ESP_OK) {
    s_srv.send(404, "application/json", String("{\"ok\":false,\"error\":\"") + esp_err_to_name(err) + "\"}");
    return;
  }
  size_t n = (size_t)snprintf(s_reply, REPLY_CAP,
                              "{\"ok\":true,\"task\":\"%.16s\",\"pc\":\"0x%08lx\",\"cause\":%lu,\"vaddr\":\"0x%08lx\","
                              "\"elf_sha\":\"%.16s\",\"bt_corrupt\":%s,\"bt\":[",
                              sum.exc_task, (unsigned long)sum.exc_pc, (unsigned long)sum.ex_info.exc_cause,
                              (unsigned long)sum.ex_info.exc_vaddr, (const char *)sum.app_elf_sha256,
                              sum.exc_bt_info.corrupted ? "true" : "false");
  for (uint32_t i = 0; i < sum.exc_bt_info.depth && i < 16 && n < REPLY_CAP - 16; i++)
    n += (size_t)snprintf(s_reply + n, REPLY_CAP - n, "%s\"0x%08lx\"", i ? "," : "", (unsigned long)sum.exc_bt_info.bt[i]);
  n += (size_t)snprintf(s_reply + n, REPLY_CAP - n, "]}");
  s_srv.send_P(200, "application/json", s_reply, n);
}

static void http_routes() {
  s_srv.collectHeaders(HDR_KEYS, sizeof HDR_KEYS / sizeof HDR_KEYS[0]);
  s_srv.on("/", HTTP_GET, handle_root);
  s_srv.on("/update", HTTP_GET, handle_update_page);
  s_srv.on("/update", HTTP_POST, handle_update_done, handle_update_upload);
  s_srv.on("/api/info", HTTP_GET, handle_info);
  s_srv.on("/api/sync", HTTP_POST, handle_sync);
  s_srv.on("/api/config", HTTP_POST, handle_config);
  s_srv.on("/api/icon", HTTP_POST, image_done, handle_icon_upload);
  s_srv.on("/api/backdrop", HTTP_POST, image_done, handle_backdrop_upload);
  s_srv.on("/api/screen", HTTP_GET, handle_screen);
  s_srv.on("/api/tap", HTTP_POST, handle_tap);
  s_srv.on("/api/debug/gpio", HTTP_POST, handle_debug_gpio);
  s_srv.on("/api/debug/bl", HTTP_POST, handle_debug_bl);
  s_srv.on("/api/debug/pclk", HTTP_POST, handle_debug_pclk);
  s_srv.on("/api/debug/freeze", HTTP_POST, handle_debug_freeze);
  s_srv.on("/api/debug/dispcfg", HTTP_POST, handle_debug_dispcfg);
  s_srv.on("/api/debug/flip", HTTP_POST, handle_debug_flip);
  s_srv.on("/api/debug/coredump", HTTP_GET, handle_debug_coredump);
  s_srv.onNotFound([] { s_srv.send(404, "text/plain", "not found"); });
}

/* ---- link task: USB frames + HTTP, one context, never the UI's ------------ */
static void link_task(void *) {
  for (;;) {
    bool busy = usb_link_poll();
    if (!s_http_started && WiFi.status() == WL_CONNECTED) {
      s_srv.begin();
      s_http_started = true;
      pd_log("[net] http server up\n");
    }
    if (s_http_started) s_srv.handleClient();
    vTaskDelay(busy ? 1 : 2);
  }
}

/* ---- Wi-Fi ----------------------------------------------------------------- */
static bool stored_ssid(char *buf, size_t n) {
  wifi_config_t c = {};
  if (esp_wifi_get_config(WIFI_IF_STA, &c) != ESP_OK || !c.sta.ssid[0]) return false;
  if (buf) snprintf(buf, n, "%.32s", (const char *)c.sta.ssid);
  return true;
}

static void wifi_connect() {
  Settings s = settings_get();
  if (s.ssid[0]) {
    s_wifi_src = "app";
    WiFi.begin(s.ssid, s.pass);
    return;
  }
#ifdef WIFI_SSID
  if (WIFI_SSID[0]) {
    s_wifi_src = "build";
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    return;
  }
#endif
  if (stored_ssid(nullptr, 0)) {
    s_wifi_src = "stored";
    WiFi.begin(); /* the network the ESP32's Wi-Fi storage remembers */
    return;
  }
  s_wifi_src = "none";
}

static void start_mdns() {
  MDNS.end();
  s_mdns_up = MDNS.begin(PANDA_HOSTNAME);
  if (!s_mdns_up) return;
  Settings s = settings_get();
  MDNS.setInstanceName(s.name);
  MDNS.addService("http", "tcp", 80);
  MDNS.addServiceTxt("http", "tcp", "fw", FW_VERSION);
  MDNS.addServiceTxt("http", "tcp", "app", "pandadeck");
}

void net_init() {
  s_reply = (char *)heap_caps_malloc(REPLY_CAP, MALLOC_CAP_SPIRAM);
  /* Never write credentials into the ESP32's own Wi-Fi storage: whatever
   * network it remembers (the stock firmware's) stays the fallback. */
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(PANDA_HOSTNAME);
  WiFi.setSleep(false); /* latency over power: it's on USB */
  WiFi.setAutoReconnect(true);
  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t) {
    start_mdns();
    pd_log("[net] up %s rssi=%d (%s)\n", WiFi.localIP().toString().c_str(), WiFi.RSSI(), s_wifi_src);
  }, ARDUINO_EVENT_WIFI_STA_GOT_IP);
  wifi_connect();
  Settings s = settings_get();
  configTzTime(s.tz, "pool.ntp.org", "time.google.com");

  http_routes();
  xTaskCreatePinnedToCore(link_task, "link", LINK_TASK_STACK, nullptr, 3, &s_task, 0);
}

void net_apply_wifi() {
  WiFi.disconnect(false, false);
  wifi_connect();
  pd_log("[net] wifi settings applied (%s)\n", s_wifi_src);
}

void net_set_name(const char *name) {
  if (s_mdns_up) MDNS.setInstanceName(name);
}

bool net_connected() { return WiFi.status() == WL_CONNECTED; }
int net_rssi() { return WiFi.RSSI(); }
bool net_wifi_configured() { return strcmp(s_wifi_src, "none") != 0; }
const char *net_wifi_source() { return s_wifi_src; }
const char *net_hostname() { return PANDA_HOSTNAME; }

bool net_ip(char *buf, size_t n) {
  if (!n) return false;
  buf[0] = '\0';
  if (!net_connected()) return false;
  IPAddress ip = WiFi.localIP();
  snprintf(buf, n, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
  return true;
}

void net_ssid(char *buf, size_t n) {
  if (!n) return;
  buf[0] = '\0';
  if (net_connected()) {
    snprintf(buf, n, "%s", WiFi.SSID().c_str());
    return;
  }
  if (!strcmp(s_wifi_src, "app")) {
    Settings s = settings_get();
    snprintf(buf, n, "%s", s.ssid);
#ifdef WIFI_SSID
  } else if (!strcmp(s_wifi_src, "build")) {
    snprintf(buf, n, "%s", WIFI_SSID);
#endif
  } else if (!strcmp(s_wifi_src, "stored")) {
    stored_ssid(buf, n);
  }
}

bool net_time_ok() {
  time_t now = time(nullptr);
  return now > 1700000000;
}

bool net_marked_valid() { return s_marked_valid; }

uint32_t net_link_stack_free() { return s_task ? (uint32_t)uxTaskGetStackHighWaterMark(s_task) : 0; }

void net_mark_valid_if_healthy() {
  if (s_marked_valid || millis() < 20000) return;
  if (!net_connected() && !usb_link_seen()) return;
  esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
  s_marked_valid = true;
  pd_log("[ota] marked app valid (%s)\n", esp_err_to_name(err));
}
