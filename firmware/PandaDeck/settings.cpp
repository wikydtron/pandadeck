#include "settings.h"

#include "display.h"
#include "pdlog.h"

#include <Arduino.h>
#include <Preferences.h>
#include <esp_random.h>
#include <freertos/semphr.h>
#include <string.h>

#define NVS_NS "pd"
#define TOKEN_LEN 24  /* generated tokens */
#define TOKEN_CAP 65  /* a build token (config.h) may be longer */

static SemaphoreHandle_t s_mu = nullptr;
static Settings s_cur;
static volatile uint32_t s_rev = 1, s_pages_rev = 1;
static char s_token[TOKEN_CAP];
#ifdef PANDA_TOKEN
static_assert(sizeof(PANDA_TOKEN) <= TOKEN_CAP, "PANDA_TOKEN: 64 characters max");
#endif

struct Lock {
  Lock() { xSemaphoreTake(s_mu, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(s_mu); }
};

static void get_str(Preferences &p, const char *key, char *dst, size_t cap, const char *dflt) {
  dst[0] = '\0';
  if (p.isKey(key)) p.getString(key, dst, cap);
  if (!dst[0] && dflt) {
    strncpy(dst, dflt, cap - 1);
    dst[cap - 1] = '\0';
  }
}

static void make_token(char *out) {
  static const char ALPHA[] = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
  for (int i = 0; i < TOKEN_LEN; i++) out[i] = ALPHA[esp_random() % (sizeof ALPHA - 1)];
  out[TOKEN_LEN] = '\0';
}

static void load_token(Preferences &p) {
#ifdef PANDA_TOKEN
  (void)p;
  strncpy(s_token, PANDA_TOKEN, sizeof s_token - 1);
  s_token[sizeof s_token - 1] = '\0';
#else
  get_str(p, "token", s_token, TOKEN_LEN + 1, nullptr);
  if (strlen(s_token) != TOKEN_LEN) {
    make_token(s_token);
    p.putString("token", s_token);
    pd_log("[cfg] generated a new API token\n");
  }
#endif
}

void settings_init() {
  if (!s_mu) s_mu = xSemaphoreCreateMutex();
  memset(&s_cur, 0, sizeof s_cur);
  Preferences p;
  bool ok = p.begin(NVS_NS, false);
  char csv[96] = "";
  if (ok) get_str(p, "pages", csv, sizeof csv, nullptr);
  if (csv[0]) pages_from_csv(csv, &s_cur.pages);
  else pages_default(&s_cur.pages);
  s_cur.brightness = ok ? p.getUChar("bright", 100) : 100;
  if (s_cur.brightness < PD_MIN_BRIGHTNESS || s_cur.brightness > 100) s_cur.brightness = 100;
  if (ok) {
    get_str(p, "name", s_cur.name, sizeof s_cur.name, "PandaDeck");
    get_str(p, "tz", s_cur.tz, sizeof s_cur.tz, PANDA_TZ);
    get_str(p, "ssid", s_cur.ssid, sizeof s_cur.ssid, nullptr);
    get_str(p, "pass", s_cur.pass, sizeof s_cur.pass, nullptr);
    load_token(p);
    p.end();
  } else {
    strcpy(s_cur.name, "PandaDeck");
    strncpy(s_cur.tz, PANDA_TZ, sizeof s_cur.tz - 1);
#ifdef PANDA_TOKEN
    strncpy(s_token, PANDA_TOKEN, sizeof s_token - 1);
#else
    make_token(s_token); /* NVS unusable: valid for this boot only */
#endif
    pd_log("[cfg] NVS namespace unavailable, using defaults\n");
  }
}

Settings settings_get() {
  Lock l;
  Settings s = s_cur;
  s.flip = display_flip_wanted();
  s.tflip = display_touch_flipped();
  return s;
}

uint32_t settings_rev() { return s_rev; }
uint32_t settings_pages_rev() { return s_pages_rev; }

static void persist(const SettingsPatch &p, const Settings &s) {
  Preferences nv;
  if (!nv.begin(NVS_NS, false)) {
    pd_log("[cfg] NVS write failed\n");
    return;
  }
  if (p.has_pages) {
    char csv[96];
    pages_to_csv(s.pages, csv, sizeof csv);
    nv.putString("pages", csv);
  }
  if (p.has_brightness) nv.putUChar("bright", s.brightness);
  if (p.has_name) nv.putString("name", s.name);
  if (p.has_tz) nv.putString("tz", s.tz);
  if (p.has_wifi) {
    if (s.ssid[0]) {
      nv.putString("ssid", s.ssid);
      nv.putString("pass", s.pass);
    } else {
      nv.remove("ssid");
      nv.remove("pass");
    }
  }
  nv.end();
}

void settings_apply(const SettingsPatch &p) {
  bool pages_changed;
  Settings after;
  {
    Lock l; /* short: the UI reads under this lock every tick */
    PageList before = s_cur.pages;
    proto_apply_settings(&s_cur, p);
    pages_changed = p.has_pages && (before.n != s_cur.pages.n ||
                                    memcmp(before.ids, s_cur.pages.ids, before.n) != 0);
    after = s_cur;
  }
  persist(p, after); /* flash write outside the lock */
  /* display.cpp persists these in its own namespace */
  if (p.has_flip) display_request_flip(p.flip);
  if (p.has_tflip) display_set_touch_flip(p.tflip);
  if (pages_changed) s_pages_rev = s_pages_rev + 1;
  s_rev = s_rev + 1;
}

const char *settings_token() { return s_token; }

bool settings_token_from_build() {
#ifdef PANDA_TOKEN
  return true;
#else
  return false;
#endif
}

void settings_factory_reset() {
  Preferences p;
  if (p.begin(NVS_NS, false)) {
    p.clear();
    p.end();
  }
  display_forget_orientation();
  pd_log("[cfg] settings reset\n");
}
