#include "proto.h"

#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>

/* ---- pages ----------------------------------------------------------------- */
struct PageDef {
  const char *name, *label;
};

static const PageDef PAGES[PAGE_ID_COUNT] = {
    {"ai", "AI"},
    {"deck", "DECK"},
    {"music", "MUSIC"},
    {"hud", "HUD"},
};

int page_id_from_name(const char *name) {
  if (!name) return -1;
  for (int i = 0; i < PAGE_ID_COUNT; i++)
    if (!strcmp(PAGES[i].name, name)) return i;
  return -1;
}

const char *page_id_name(uint8_t id) { return id < PAGE_ID_COUNT ? PAGES[id].name : ""; }
const char *page_id_label(uint8_t id) { return id < PAGE_ID_COUNT ? PAGES[id].label : ""; }

bool pages_contains(const PageList &l, uint8_t id) {
  for (uint8_t i = 0; i < l.n; i++)
    if (l.ids[i] == id) return true;
  return false;
}

void pages_add(PageList *l, const char *name) {
  int id = page_id_from_name(name);
  if (id < 0 || l->n >= PAGE_ID_COUNT || pages_contains(*l, (uint8_t)id)) return;
  l->ids[l->n++] = (uint8_t)id;
}

void pages_finish(PageList *l) {
  if (l->n == 0) l->ids[l->n++] = PAGE_ID_DECK;
}

void pages_default(PageList *out) {
  out->n = 0;
  pages_add(out, "deck");
  pages_add(out, "music");
  pages_add(out, "hud");
}

void pages_from_csv(const char *csv, PageList *out) {
  out->n = 0;
  char tok[16];
  size_t t = 0;
  for (const char *p = csv ? csv : "";; p++) {
    if (*p == ',' || *p == '\0') {
      tok[t] = '\0';
      pages_add(out, tok);
      t = 0;
      if (*p == '\0') break;
    } else if (t + 1 < sizeof tok) {
      tok[t++] = *p;
    }
  }
  pages_finish(out);
}

size_t pages_to_csv(const PageList &l, char *buf, size_t cap) {
  size_t n = 0;
  if (cap) buf[0] = '\0';
  for (uint8_t i = 0; i < l.n; i++) {
    int r = snprintf(buf + n, cap - n, "%s%s", i ? "," : "", page_id_name(l.ids[i]));
    if (r < 0 || (size_t)r >= cap - n) break;
    n += (size_t)r;
  }
  return n;
}

/* ---- settings -------------------------------------------------------------- */
static bool printable(const char *s) {
  for (; *s; s++)
    if ((unsigned char)*s < 0x20 || *s == 0x7F) return false;
  return true;
}

/* String value of at most cap-1 bytes, printable; false otherwise. */
static bool take_str(JsonVariantConst v, char *dst, size_t cap, size_t min_len) {
  if (!v.is<const char *>()) return false;
  const char *s = v.as<const char *>();
  size_t n = strlen(s);
  if (n < min_len || n >= cap || !printable(s)) return false;
  memcpy(dst, s, n + 1);
  return true;
}

static bool take_bool(JsonVariantConst v, bool *dst) {
  if (!v.is<bool>()) return false;
  *dst = v.as<bool>();
  return true;
}

static bool parse_pages(JsonVariantConst v, PageList *out) {
  if (!v.is<JsonArrayConst>()) return false;
  out->n = 0;
  for (JsonVariantConst e : v.as<JsonArrayConst>()) {
    if (!e.is<const char *>()) return false;
    pages_add(out, e.as<const char *>()); /* unknown ids are ignored */
  }
  pages_finish(out);
  return true;
}

static bool parse_wifi(JsonVariantConst v, SettingsPatch *p) {
  if (!v.is<JsonObjectConst>()) return false;
  JsonObjectConst o = v.as<JsonObjectConst>();
  if (!take_str(o["ssid"], p->ssid, sizeof p->ssid, 0)) return false;
  p->pass[0] = '\0';
  if (!o["pass"].isNull()) {
    if (!take_str(o["pass"], p->pass, sizeof p->pass, 0)) return false;
    size_t n = strlen(p->pass);
    if (n != 0 && n < 8) return false; /* WPA needs 8..63 chars (or 64 hex) */
  }
  if (!p->ssid[0]) p->pass[0] = '\0';
  return true;
}

bool proto_parse_settings(const char *json, size_t len, SettingsPatch *out, const char **err) {
  static const char *dummy;
  if (!err) err = &dummy;
  memset(out, 0, sizeof *out);
  JsonDocument doc;
  if (deserializeJson(doc, json, len) || !doc.is<JsonObjectConst>()) {
    *err = "bad json";
    return false;
  }
  JsonObjectConst o = doc.as<JsonObjectConst>();
  static const char *const KNOWN[] = {"pages", "wifi", "brightness", "flip", "tflip", "name", "tz", "reset", "bootloader"};
  for (JsonPairConst kv : o) {
    bool known = false;
    for (const char *k : KNOWN) known = known || !strcmp(kv.key().c_str(), k);
    if (!known) {
      strncpy(out->unknown, kv.key().c_str(), sizeof out->unknown - 1);
      return *err = "unknown", false;
    }
  }
  if (!o["pages"].isNull()) {
    if (!parse_pages(o["pages"], &out->pages)) return *err = "bad pages", false;
    out->has_pages = true;
  }
  if (!o["wifi"].isNull()) {
    if (!parse_wifi(o["wifi"], out)) return *err = "bad wifi", false;
    out->has_wifi = true;
  }
  if (!o["brightness"].isNull()) {
    JsonVariantConst b = o["brightness"];
    if (!(b.is<int>() || b.is<float>())) return *err = "bad brightness", false;
    float f = b.as<float>();
    if (f < 0 || f > 100) return *err = "bad brightness", false;
    int v = (int)(f + 0.5f);
    out->brightness = (uint8_t)(v < PD_MIN_BRIGHTNESS ? PD_MIN_BRIGHTNESS : v);
    out->has_brightness = true;
  }
  if (!o["flip"].isNull()) {
    if (!take_bool(o["flip"], &out->flip)) return *err = "bad flip", false;
    out->has_flip = true;
  }
  if (!o["tflip"].isNull()) {
    if (!take_bool(o["tflip"], &out->tflip)) return *err = "bad tflip", false;
    out->has_tflip = true;
  }
  if (!o["name"].isNull()) {
    if (!take_str(o["name"], out->name, sizeof out->name, 1)) return *err = "bad name", false;
    out->has_name = true;
  }
  if (!o["tz"].isNull()) {
    if (!take_str(o["tz"], out->tz, sizeof out->tz, 1)) return *err = "bad tz", false;
    out->has_tz = true;
  }
  if (!o["reset"].isNull() && !take_bool(o["reset"], &out->reset)) return *err = "bad reset", false;
  if (!o["bootloader"].isNull() && !take_bool(o["bootloader"], &out->bootloader))
    return *err = "bad bootloader", false;
  return true;
}

void proto_apply_settings(Settings *s, const SettingsPatch &p) {
  if (p.has_pages) s->pages = p.pages;
  if (p.has_wifi) {
    memcpy(s->ssid, p.ssid, sizeof s->ssid);
    memcpy(s->pass, p.pass, sizeof s->pass);
  }
  if (p.has_brightness) s->brightness = p.brightness;
  if (p.has_flip) s->flip = p.flip;
  if (p.has_tflip) s->tflip = p.tflip;
  if (p.has_name) memcpy(s->name, p.name, sizeof s->name);
  if (p.has_tz) memcpy(s->tz, p.tz, sizeof s->tz);
}

size_t proto_settings_json(const Settings &s, char *buf, size_t cap) {
  char pages[96] = "", name[PD_NAME_LEN * 6], tz[PD_TZ_LEN * 6];
  size_t pn = 0;
  for (uint8_t i = 0; i < s.pages.n && pn + 12 < sizeof pages; i++)
    pn += (size_t)snprintf(pages + pn, sizeof pages - pn, "%s\"%s\"", i ? "," : "", page_id_name(s.pages.ids[i]));
  proto_json_escape(name, sizeof name, s.name);
  proto_json_escape(tz, sizeof tz, s.tz);
  int r = snprintf(buf, cap, "{\"pages\":[%s],\"brightness\":%u,\"flip\":%s,\"tflip\":%s,\"name\":\"%s\",\"tz\":\"%s\"}",
                   pages, (unsigned)s.brightness, s.flip ? "true" : "false", s.tflip ? "true" : "false", name, tz);
  if (r < 0 || (size_t)r >= cap) {
    if (cap) buf[0] = '\0';
    return 0;
  }
  return (size_t)r;
}

/* ---- images ---------------------------------------------------------------- */
/* The header is parsed in the USB receive callback (small stack), so this
 * is a tiny scanner for one flat JSON object instead of ArduinoJson:
 * string / integer / true / false / null values, no nesting. */
struct Scan {
  const char *p, *e;
  void ws() {
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')) p++;
  }
  bool eat(char c) {
    ws();
    if (p < e && *p == c) {
      p++;
      return true;
    }
    return false;
  }
  /* "..." into dst (cap incl. NUL); *fits = false if it was longer. */
  bool str(char *dst, size_t cap, bool *fits) {
    if (!eat('"')) return false;
    size_t n = 0;
    *fits = true;
    while (p < e && *p != '"') {
      char c = *p++;
      if (c == '\\') {
        if (p >= e) return false;
        char x = *p++;
        c = x == 'n' ? '\n' : x == 't' ? '\t' : x == 'r' ? '\r' : x == 'b' ? '\b' : x == 'f' ? '\f' : x;
        if (x == 'u') return false; /* not needed for headers */
      }
      if ((unsigned char)c < 0x20) return false;
      if (n + 1 < cap) dst[n++] = c;
      else *fits = false;
    }
    if (cap) dst[n] = '\0';
    return eat('"');
  }
  bool num(long *out) {
    ws();
    bool neg = p < e && *p == '-';
    if (neg) p++;
    if (p >= e || *p < '0' || *p > '9') return false;
    long v = 0;
    while (p < e && *p >= '0' && *p <= '9') {
      if (v < 100000000L) v = v * 10 + (*p - '0');
      p++;
    }
    while (p < e && ((*p >= '0' && *p <= '9') || *p == '.' || *p == 'e' || *p == 'E' || *p == '+' || *p == '-')) p++;
    *out = neg ? -v : v;
    return true;
  }
  bool word(const char *w) {
    size_t n = strlen(w);
    if ((size_t)(e - p) < n || strncmp(p, w, n)) return false;
    p += n;
    return true;
  }
};

bool proto_parse_image_header(const char *json, size_t len, ImageHeader *out, const char **err) {
  static const char *dummy;
  if (!err) err = &dummy;
  memset(out, 0, sizeof *out);
  Scan s{json, json + len};
  char kind[12] = "", key[12], val[PROTO_IMG_KEY_LEN];
  bool key_ok = false, tag_ok = true, fits;
  long w = 0, h = 0;
  if (!s.eat('{')) return *err = "bad header", false;
  if (!s.eat('}')) {
    for (;;) {
      if (!s.str(key, sizeof key, &fits) || !s.eat(':')) return *err = "bad header", false;
      if (!fits) key[0] = '\0'; /* unknown long key: value is skipped */
      s.ws();
      if (s.p < s.e && *s.p == '"') {
        if (!s.str(val, sizeof val, &fits)) return *err = "bad header", false;
        /* plain copies: no printf family here, it needs ~1 KB of stack */
        if (!strcmp(key, "kind")) {
          if (fits && strlen(val) < sizeof kind) memcpy(kind, val, strlen(val) + 1);
          else kind[0] = '\0';
        } else if (!strcmp(key, "key")) {
          key_ok = fits && val[0];
          if (key_ok) memcpy(out->key, val, sizeof out->key);
        } else if (!strcmp(key, "tag")) {
          tag_ok = fits && strlen(val) < sizeof out->tag;
          if (tag_ok) memcpy(out->tag, val, strlen(val) + 1);
        }
      } else if (s.p < s.e && (*s.p == '-' || (*s.p >= '0' && *s.p <= '9'))) {
        long v;
        if (!s.num(&v)) return *err = "bad header", false;
        if (!strcmp(key, "w")) w = v;
        else if (!strcmp(key, "h")) h = v;
      } else if (!(s.word("true") || s.word("false") || s.word("null"))) {
        return *err = "bad header", false; /* nested objects / arrays */
      }
      if (s.eat(',')) continue;
      if (s.eat('}')) break;
      return *err = "bad header", false;
    }
  }
  s.ws();
  if (s.p != s.e) return *err = "bad header", false;
  if (!strcmp(kind, "icon")) {
    out->kind = IMG_ICON;
    if (!key_ok) return *err = "bad key", false;
  } else if (!strcmp(kind, "backdrop")) {
    out->kind = IMG_BACKDROP;
  } else {
    return *err = "bad kind", false;
  }
  if (!tag_ok) return *err = "bad tag", false;
  if (w <= 0 || h <= 0 || w > 0xFFFF || h > 0xFFFF) return *err = "bad size", false;
  out->w = (uint16_t)w;
  out->h = (uint16_t)h;
  return true;
}

uint32_t proto_image_bytes(const ImageHeader &h) {
  return (uint32_t)h.w * h.h * (h.kind == IMG_ICON ? 3u : 2u);
}

/* ---- update ---------------------------------------------------------------- */
static bool is_hex32(const char *s) {
  if (strlen(s) != 32) return false;
  for (; *s; s++)
    if (!((*s >= '0' && *s <= '9') || (*s >= 'a' && *s <= 'f') || (*s >= 'A' && *s <= 'F'))) return false;
  return true;
}

bool proto_parse_update(const char *json, size_t len, UpdateCmd *out, const char **err) {
  static const char *dummy;
  if (!err) err = &dummy;
  memset(out, 0, sizeof *out);
  JsonDocument doc;
  if (deserializeJson(doc, json, len) || !doc.is<JsonObjectConst>()) return *err = "bad json", false;
  JsonObjectConst o = doc.as<JsonObjectConst>();
  if (o["begin"].is<JsonObjectConst>()) {
    JsonObjectConst b = o["begin"].as<JsonObjectConst>();
    out->kind = UPD_BEGIN;
    long size = b["size"] | 0L;
    if (size <= 0) return *err = "bad size", false;
    out->size = (uint32_t)size;
    const char *md5 = b["md5"] | "";
    if (*md5 && !is_hex32(md5)) return *err = "bad md5", false;
    for (int i = 0; md5[i] && i < 32; i++) out->md5[i] = (char)(md5[i] >= 'A' && md5[i] <= 'F' ? md5[i] + 32 : md5[i]);
    return true;
  }
  if (o["end"] | false) return out->kind = UPD_END, true;
  if (o["abort"] | false) return out->kind = UPD_ABORT, true;
  return *err = "expected begin/end", false;
}

/* ---- JSON ------------------------------------------------------------------ */
size_t proto_json_escape(char *dst, size_t cap, const char *s) {
  size_t n = 0;
  if (!cap) return 0;
  for (; s && *s; s++) {
    unsigned char c = (unsigned char)*s;
    char esc[8];
    size_t k;
    if (c == '"' || c == '\\') {
      esc[0] = '\\';
      esc[1] = (char)c;
      k = 2;
    } else if (c < 0x20) {
      k = (size_t)snprintf(esc, sizeof esc, "\\u%04x", c);
    } else {
      esc[0] = (char)c;
      k = 1;
    }
    if (n + k + 1 > cap) break;
    memcpy(dst + n, esc, k);
    n += k;
  }
  dst[n] = '\0';
  return n;
}
