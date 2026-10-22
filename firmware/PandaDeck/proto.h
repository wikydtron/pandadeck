/* Link payload helpers shared by the USB and HTTP transports: page ids,
 * settings (parse a partial `C` / POST /api/config update, serialize the
 * current values), the `I` image header and the `U` update commands.
 * Pure C++ + ArduinoJson: host-tested (tests/model_tests.cpp). */
#pragma once
#include "features.h"

#include <stddef.h>
#include <stdint.h>

/* ---- pages ----------------------------------------------------------------- */
enum PageId : uint8_t {
  PAGE_ID_AI = 0,
  PAGE_ID_DECK,
  PAGE_ID_MUSIC,
  PAGE_ID_HUD,
  PAGE_ID_COUNT
};

struct PageList {
  uint8_t n;
  uint8_t ids[PAGE_ID_COUNT]; /* PageId, in tab order, no duplicates */
};

/* "ai" -> PAGE_ID_AI; -1 for ids this build doesn't have. */
int page_id_from_name(const char *name);
const char *page_id_name(uint8_t id);  /* "ai" */
const char *page_id_label(uint8_t id); /* "AI" (tab text) */
/* Default tab list for a panel that never got one. */
void pages_default(PageList *out);
/* Adds a page if known and not yet listed. */
void pages_add(PageList *l, const char *name);
/* An empty list falls back to ["deck"] (protocol rule). */
void pages_finish(PageList *l);
/* "ai,deck,music" <-> PageList (NVS storage format). */
void pages_from_csv(const char *csv, PageList *out);
size_t pages_to_csv(const PageList &l, char *buf, size_t cap);
bool pages_contains(const PageList &l, uint8_t id);

/* ---- settings -------------------------------------------------------------- */
#define PD_NAME_LEN 32
#define PD_TZ_LEN 48
#define PD_SSID_LEN 33
#define PD_PASS_LEN 65
#define PD_MIN_BRIGHTNESS 5 /* never let a setting black the screen out */

struct Settings {
  PageList pages;
  uint8_t brightness; /* PD_MIN_BRIGHTNESS..100 */
  bool flip, tflip;   /* picture / touch rotated 180 deg */
  char name[PD_NAME_LEN];
  char tz[PD_TZ_LEN];   /* POSIX TZ, e.g. "EST5EDT,M3.2.0,M11.1.0" */
  char ssid[PD_SSID_LEN]; /* Wi-Fi stored by the app ("" = none) */
  char pass[PD_PASS_LEN];
};

/* A partial update: has_* says which keys were present. */
struct SettingsPatch {
  bool has_pages;
  PageList pages;
  bool has_wifi;
  char ssid[PD_SSID_LEN]; /* "" = forget */
  char pass[PD_PASS_LEN];
  bool has_brightness;
  uint8_t brightness;
  bool has_flip, flip;
  bool has_tflip, tflip;
  bool has_name;
  char name[PD_NAME_LEN];
  bool has_tz;
  char tz[PD_TZ_LEN];
  bool reset;      /* {"reset":true}: forget PandaDeck's settings + reboot */
  bool bootloader; /* {"bootloader":true}: reboot into the ROM download mode */
  char unknown[24]; /* first key this firmware doesn't know (error "unknown") */
};

/* Parses a settings update. Wrong types / out-of-range values reject the
 * whole update (*err says which key). A key this firmware doesn't know also
 * rejects it, with *err = "unknown" and out->unknown = the key, so the app
 * can tell the user the firmware is too old for that setting. */
bool proto_parse_settings(const char *json, size_t len, SettingsPatch *out, const char **err);
/* Merges the present keys of p into s. */
void proto_apply_settings(Settings *s, const SettingsPatch &p);
/* {"pages":[..],"brightness":N,"flip":b,"tflip":b,"name":"..","tz":".."}
 * (never the Wi-Fi password). Returns the length (0 if it didn't fit). */
size_t proto_settings_json(const Settings &s, char *buf, size_t cap);

/* ---- images (`I` frame header line) ---------------------------------------- */
enum ImageKind : uint8_t { IMG_ICON = 0, IMG_BACKDROP = 1 };
#define PROTO_IMG_KEY_LEN 16
#define PROTO_IMG_TAG_LEN 12

struct ImageHeader {
  uint8_t kind;
  char key[PROTO_IMG_KEY_LEN]; /* icon only */
  char tag[PROTO_IMG_TAG_LEN];
  uint16_t w, h;
};

bool proto_parse_image_header(const char *json, size_t len, ImageHeader *out, const char **err);
/* Pixel bytes that must follow the header: icon RGB565A8 = w*h*3,
 * backdrop RGB565 = w*h*2. */
uint32_t proto_image_bytes(const ImageHeader &h);

/* ---- firmware update (`U` frame JSON) -------------------------------------- */
enum UpdateCmdKind : uint8_t { UPD_BEGIN, UPD_END, UPD_ABORT };
struct UpdateCmd {
  uint8_t kind;
  uint32_t size; /* begin: image size in bytes */
  char md5[33];  /* begin: "" or 32 lowercase hex chars */
};
bool proto_parse_update(const char *json, size_t len, UpdateCmd *out, const char **err);

/* ---- JSON ------------------------------------------------------------------ */
/* Escapes s for a JSON string body (quotes not added). Always terminates. */
size_t proto_json_escape(char *dst, size_t cap, const char *s);
