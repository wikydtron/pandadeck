// Host tests for the USB link framing (link_codec.cpp) and the payload
// helpers shared by USB and HTTP (proto.cpp). Build + run: tests/run_host_tests.sh
#include <ArduinoJson.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "link_codec.h"
#include "proto.h"

static int g_fail = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    if (!(cond)) {                                                      \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);       \
      g_fail++;                                                         \
    }                                                                   \
  } while (0)

/* ---- helpers ----------------------------------------------------------------- */
struct Frame {
  char type;
  bool accepted;
  std::string payload;
};

class RecSink : public LinkSink {
 public:
  std::vector<Frame> frames;
  std::string cur;
  int aborts = 0;
  bool accept = true;
  uint32_t begun_len = 0;
  bool frame_begin(char, uint32_t len) override {
    cur.clear();
    begun_len = len;
    return accept;
  }
  void frame_data(const uint8_t *p, size_t n) override { cur.append((const char *)p, n); }
  void frame_end(char t, bool acc) override { frames.push_back({t, acc, cur}); }
  void frame_abort(char) override { aborts++; }
};

static std::string frame(char type, const std::string &payload) {
  uint8_t h[LINK_HEADER_LEN];
  link_encode_header(h, type, (uint32_t)payload.size());
  return std::string((const char *)h, sizeof h) + payload;
}

/* Feeds everything (in `chunk`-byte pieces), looping like the firmware does. */
static void feed_all(LinkDecoder &d, RecSink &s, const std::string &bytes, size_t chunk, uint32_t now = 0) {
  size_t off = 0;
  while (off < bytes.size()) {
    size_t n = bytes.size() - off < chunk ? bytes.size() - off : chunk;
    size_t used = d.feed((const uint8_t *)bytes.data() + off, n, s, now);
    CHECK(used > 0);
    if (!used) return;
    off += used;
  }
}

/* ---- framing ------------------------------------------------------------------- */
static void test_header_encoding() {
  uint8_t h[LINK_HEADER_LEN];
  link_encode_header(h, 'S', 0x01020304u);
  const uint8_t want[] = {0x1B, 'P', 'D', 'S', 0x04, 0x03, 0x02, 0x01};
  CHECK(!memcmp(h, want, sizeof want));
  CHECK(link_reply_type('S') == 's' && link_reply_type('H') == 'h' && link_reply_type('d') == 'd');
}

static void test_decodes_one_frame_and_stops_after_it() {
  LinkDecoder d;
  RecSink s;
  std::string two = frame('S', "{\"t\":1}") + frame('P', "");
  size_t used = d.feed((const uint8_t *)two.data(), two.size(), s, 0);
  CHECK(used == LINK_HEADER_LEN + 7);  // returns right after the first frame
  CHECK(s.frames.size() == 1 && s.frames[0].type == 'S' && s.frames[0].payload == "{\"t\":1}");
  used += d.feed((const uint8_t *)two.data() + used, two.size() - used, s, 0);
  CHECK(used == two.size());
  CHECK(s.frames.size() == 2 && s.frames[1].type == 'P' && s.frames[1].payload.empty());
  CHECK(d.frames() == 2 && !d.in_frame());
}

static void test_any_chunking_gives_the_same_payload() {
  std::string payload;
  for (int i = 0; i < 5000; i++) payload.push_back((char)(i * 37 + 11));
  std::string bytes = frame('I', payload);
  for (size_t chunk : {1u, 2u, 3u, 7u, 8u, 9u, 64u, 4096u, 100000u}) {
    LinkDecoder d;
    RecSink s;
    feed_all(d, s, bytes, chunk);
    CHECK(s.frames.size() == 1 && s.frames[0].payload == payload);
  }
}

static void test_skips_log_text_and_false_magic() {
  LinkDecoder d;
  RecSink s;
  std::string noise = "[boot] PandaDeck 0.6.0\r\n\x1BP nope \x1B\x1BPx \x1B";
  std::string bytes = noise + "\x1B" + frame('H', "{}").substr(1) + "[net] up\n";
  feed_all(d, s, bytes, 5);
  CHECK(s.frames.size() == 1 && s.frames[0].type == 'H' && s.frames[0].payload == "{}");
}

static void test_bad_headers_are_dropped_then_resync() {
  LinkDecoder d(1024);
  RecSink s;
  std::string bad_type = std::string("\x1BPD") + '\x01' + std::string("\x02\0\0\0", 4);
  std::string too_big = frame('S', "").substr(0, 4) + std::string("\x01\x04\0\0", 4); /* 1025 > 1024 */
  std::string ok = frame('C', "{\"brightness\":50}");
  feed_all(d, s, bad_type + too_big + ok, 3);
  CHECK(s.frames.size() == 1 && s.frames[0].type == 'C');
  CHECK(d.dropped() == 2);
}

static void test_rejected_frame_is_skipped_but_ended() {
  LinkDecoder d;
  RecSink s;
  s.accept = false;
  feed_all(d, s, frame('X', std::string(300, 'z')), 64);
  CHECK(s.frames.size() == 1 && !s.frames[0].accepted && s.frames[0].payload.empty());
  s.accept = true;
  feed_all(d, s, frame('P', ""), 64);
  CHECK(s.frames.size() == 2 && s.frames[1].accepted);
}

static void test_stalled_frame_times_out_and_next_request_decodes() {
  LinkDecoder d;
  RecSink s;
  std::string big = frame('S', std::string(100, 'a'));
  d.feed((const uint8_t *)big.data(), 40, s, 1000);  // lost the rest on the wire
  CHECK(d.in_frame());
  CHECK(!d.check_timeout(1500, 1000, s));  // still inside the window
  CHECK(d.check_timeout(2001, 1000, s));
  CHECK(s.aborts == 1 && !d.in_frame() && d.dropped() == 1);
  feed_all(d, s, frame('P', ""), 64, 3000);
  CHECK(s.frames.size() == 1 && s.frames[0].type == 'P');
}

/* The firmware reads exactly need() bytes at a time from the CDC port, so a
 * finished frame never leaves bytes of the next request in its hands. */
static void test_need_never_reads_past_a_frame() {
  LinkDecoder d;
  RecSink s;
  std::string bytes = std::string("log\x1B line\n\x1B") + frame('P', "") + frame('S', std::string(70, 's')) +
                      "\x1BP" + frame('H', "{}");
  size_t off = 0, frames_seen = 0;
  while (off < bytes.size()) {
    size_t n = d.need();
    CHECK(n > 0);
    if (n > bytes.size() - off) n = bytes.size() - off;
    size_t used = d.feed((const uint8_t *)bytes.data() + off, n, s, 0);
    CHECK(used == n);  // nothing left over, ever
    off += used;
    if (s.frames.size() != frames_seen) {
      frames_seen = s.frames.size();
      CHECK(!d.in_frame());
    }
  }
  CHECK(s.frames.size() == 3 && s.frames[0].type == 'P' && s.frames[1].payload.size() == 70 &&
        s.frames[2].payload == "{}");
}

static void test_max_payload_streams_without_buffering() {
  LinkDecoder d;
  RecSink s;
  uint8_t h[LINK_HEADER_LEN];
  link_encode_header(h, 'D', LINK_MAX_PAYLOAD);
  CHECK(d.feed(h, sizeof h, s, 0) == sizeof h && d.in_frame() && s.begun_len == LINK_MAX_PAYLOAD);
  std::vector<uint8_t> chunk(1000, 0x5A);
  uint32_t left = LINK_MAX_PAYLOAD;
  while (left) {
    size_t n = left < chunk.size() ? left : chunk.size();
    CHECK(d.feed(chunk.data(), n, s, 0) == n);
    left -= (uint32_t)n;
  }
  CHECK(s.frames.size() == 1 && s.frames[0].payload.size() == LINK_MAX_PAYLOAD);
}

/* ---- pages + settings ------------------------------------------------------------ */
static void test_pages_csv_normalizes() {
  PageList l;
  pages_from_csv("music,bogus,deck,music,hud", &l);
  CHECK(l.n == 3 && l.ids[0] == PAGE_ID_MUSIC && l.ids[1] == PAGE_ID_DECK && l.ids[2] == PAGE_ID_HUD);
  char csv[64];
  pages_to_csv(l, csv, sizeof csv);
  CHECK(!strcmp(csv, "music,deck,hud"));
  pages_from_csv("", &l);
  CHECK(l.n == 1 && l.ids[0] == PAGE_ID_DECK);  // empty falls back to deck
  pages_from_csv("nope", &l);
  CHECK(l.n == 1 && l.ids[0] == PAGE_ID_DECK);
  CHECK(page_id_from_name("ai") == PAGE_ID_AI && page_id_from_name("AI") == -1);
  CHECK(!strcmp(page_id_label(PAGE_ID_HUD), "HUD"));
}

static void test_default_pages() {
  PageList l;
  pages_default(&l);
  char csv[64];
  pages_to_csv(l, csv, sizeof csv);
  CHECK(!strcmp(csv, "deck,music,hud"));
}

static bool parse(const char *j, SettingsPatch *p, const char **err) { return proto_parse_settings(j, strlen(j), p, err); }

static void test_settings_full_update() {
  SettingsPatch p;
  const char *err = nullptr;
  CHECK(parse("{\"pages\":[\"ai\",\"deck\",\"music\",\"hud\",\"weather\"],"
              "\"wifi\":{\"ssid\":\"Home\",\"pass\":\"secret123\"},\"brightness\":2,\"flip\":true,"
              "\"tflip\":false,\"name\":\"Desk\",\"tz\":\"EST5EDT,M3.2.0,M11.1.0\"}",
              &p, &err));
  CHECK(p.has_pages && p.pages.n == 4 && p.pages.ids[0] == PAGE_ID_AI && p.pages.ids[3] == PAGE_ID_HUD);
  CHECK(p.has_wifi && !strcmp(p.ssid, "Home") && !strcmp(p.pass, "secret123"));
  CHECK(p.has_brightness && p.brightness == PD_MIN_BRIGHTNESS);  // clamped, never black
  CHECK(p.has_flip && p.flip && p.has_tflip && !p.tflip);
  CHECK(p.has_name && !strcmp(p.name, "Desk") && p.has_tz && !strncmp(p.tz, "EST5EDT", 7));
  CHECK(!p.reset && !p.bootloader);
}

static void test_settings_partial_and_merge() {
  SettingsPatch p;
  CHECK(parse("{\"brightness\":80}", &p, nullptr));
  CHECK(p.has_brightness && p.brightness == 80 && !p.has_pages && !p.has_wifi && !p.has_flip && !p.has_name);
  Settings s = {};
  pages_from_csv("deck", &s.pages);
  s.brightness = 100;
  strcpy(s.name, "PandaDeck");
  strcpy(s.ssid, "Old");
  proto_apply_settings(&s, p);
  CHECK(s.brightness == 80 && !strcmp(s.name, "PandaDeck") && !strcmp(s.ssid, "Old") && s.pages.n == 1);

  CHECK(parse("{\"wifi\":{\"ssid\":\"\"}}", &p, nullptr));  // forget
  CHECK(p.has_wifi && p.ssid[0] == '\0' && p.pass[0] == '\0');
  proto_apply_settings(&s, p);
  CHECK(s.ssid[0] == '\0');
  CHECK(parse("{\"wifi\":{\"ssid\":\"Cafe\"}}", &p, nullptr));  // open network
  CHECK(p.has_wifi && !strcmp(p.ssid, "Cafe") && p.pass[0] == '\0');
  CHECK(parse("{\"pages\":[]}", &p, nullptr) && p.pages.n == 1 && p.pages.ids[0] == PAGE_ID_DECK);
  CHECK(parse("{\"reset\":true}", &p, nullptr) && p.reset && !p.bootloader);
  CHECK(parse("{\"bootloader\":true}", &p, nullptr) && p.bootloader);
}

static void test_settings_rejects_bad_values() {
  SettingsPatch p;
  const char *err = nullptr;
  CHECK(!parse("{nope", &p, &err) && !strcmp(err, "bad json"));
  CHECK(!parse("[1,2]", &p, &err) && !strcmp(err, "bad json"));
  CHECK(!parse("{\"brightness\":\"max\"}", &p, &err) && !strcmp(err, "bad brightness"));
  CHECK(!parse("{\"brightness\":101}", &p, &err) && !strcmp(err, "bad brightness"));
  CHECK(!parse("{\"flip\":1}", &p, &err) && !strcmp(err, "bad flip"));
  CHECK(!parse("{\"pages\":\"deck\"}", &p, &err) && !strcmp(err, "bad pages"));
  CHECK(!parse("{\"pages\":[1]}", &p, &err) && !strcmp(err, "bad pages"));
  CHECK(!parse("{\"wifi\":{\"ssid\":\"x\",\"pass\":\"short\"}}", &p, &err) && !strcmp(err, "bad wifi"));
  CHECK(!parse("{\"wifi\":{\"ssid\":\"123456789012345678901234567890123\"}}", &p, &err));  // 33 bytes
  CHECK(!parse("{\"name\":\"\"}", &p, &err) && !strcmp(err, "bad name"));
  CHECK(!parse("{\"name\":\"a\\nb\"}", &p, &err) && !strcmp(err, "bad name"));
  CHECK(!parse("{\"reset\":\"yes\"}", &p, &err) && !strcmp(err, "bad reset"));
  // a key this firmware doesn't know rejects the update (the app says "update the firmware")
  CHECK(!parse("{\"brightness\":50,\"hologram\":true}", &p, &err) && !strcmp(err, "unknown"));
  CHECK(!strcmp(p.unknown, "hologram") && !p.has_brightness);
}

static void test_settings_json_round_trips() {
  Settings s = {};
  pages_from_csv("hud,deck", &s.pages);
  s.brightness = 42;
  s.flip = true;
  strcpy(s.name, "Desk \"left\"");
  strcpy(s.tz, "UTC0");
  strcpy(s.pass, "do-not-leak");
  char buf[400];
  size_t n = proto_settings_json(s, buf, sizeof buf);
  CHECK(n > 0 && !strstr(buf, "do-not-leak"));
  JsonDocument doc;
  CHECK(!deserializeJson(doc, buf, n));
  CHECK(doc["pages"].size() == 2 && !strcmp(doc["pages"][0] | "", "hud"));
  CHECK((doc["brightness"] | 0) == 42 && (doc["flip"] | false) && !(doc["tflip"] | true));
  CHECK(!strcmp(doc["name"] | "", "Desk \"left\"") && !strcmp(doc["tz"] | "", "UTC0"));
  char tiny[16];
  CHECK(proto_settings_json(s, tiny, sizeof tiny) == 0 && tiny[0] == '\0');
}

/* ---- images + update ------------------------------------------------------------- */
static void test_image_header() {
  ImageHeader h;
  const char *err = nullptr;
  const char *icon = "{\"kind\":\"icon\",\"key\":\"art\",\"w\":248,\"h\":248,\"tag\":\"2afdb03a\"}";
  CHECK(proto_parse_image_header(icon, strlen(icon), &h, &err));
  CHECK(h.kind == IMG_ICON && !strcmp(h.key, "art") && !strcmp(h.tag, "2afdb03a") && h.w == 248);
  CHECK(proto_image_bytes(h) == 248u * 248u * 3u);
  const char *bd = "{\"kind\":\"backdrop\",\"w\":200,\"h\":108}";
  CHECK(proto_parse_image_header(bd, strlen(bd), &h, &err));
  CHECK(h.kind == IMG_BACKDROP && h.tag[0] == '\0' && proto_image_bytes(h) == 200u * 108u * 2u);
  const char *nokey = "{\"kind\":\"icon\",\"w\":5,\"h\":5}";
  CHECK(!proto_parse_image_header(nokey, strlen(nokey), &h, &err) && !strcmp(err, "bad key"));
  const char *longkey = "{\"kind\":\"icon\",\"key\":\"0123456789abcdef\",\"w\":5,\"h\":5}";
  CHECK(!proto_parse_image_header(longkey, strlen(longkey), &h, &err));
  const char *kind = "{\"kind\":\"video\",\"w\":5,\"h\":5}";
  CHECK(!proto_parse_image_header(kind, strlen(kind), &h, &err) && !strcmp(err, "bad kind"));
  const char *zero = "{\"kind\":\"backdrop\",\"w\":0,\"h\":5}";
  CHECK(!proto_parse_image_header(zero, strlen(zero), &h, &err) && !strcmp(err, "bad size"));
  // the header scanner (runs in the USB RX callback, no ArduinoJson)
  const char *spaced = " { \"w\" : 56 ,\n\"h\":56.0, \"kind\" : \"icon\", \"key\":\"a\\\"b\", \"extra\":null,"
                       " \"flag\":true, \"some_long_unknown_key\":\"whatever value\", \"n\":-3 } \r\n";
  CHECK(proto_parse_image_header(spaced, strlen(spaced), &h, &err));
  CHECK(h.w == 56 && h.h == 56 && !strcmp(h.key, "a\"b") && h.tag[0] == '\0');
  const char *nested = "{\"kind\":\"icon\",\"key\":\"k\",\"w\":5,\"h\":5,\"meta\":{\"a\":1}}";
  CHECK(!proto_parse_image_header(nested, strlen(nested), &h, &err) && !strcmp(err, "bad header"));
  const char *longtag = "{\"kind\":\"backdrop\",\"w\":5,\"h\":5,\"tag\":\"0123456789ab\"}";
  CHECK(!proto_parse_image_header(longtag, strlen(longtag), &h, &err) && !strcmp(err, "bad tag"));
  const char *trailing = "{\"kind\":\"backdrop\",\"w\":5,\"h\":5} x";
  CHECK(!proto_parse_image_header(trailing, strlen(trailing), &h, &err));
  const char *unterminated = "{\"kind\":\"backdrop\",\"w\":5,\"h\":5";
  CHECK(!proto_parse_image_header(unterminated, strlen(unterminated), &h, &err));
  CHECK(!proto_parse_image_header("", 0, &h, &err) && !strcmp(err, "bad header"));
}

static void test_update_commands() {
  UpdateCmd c;
  const char *err = nullptr;
  const char *b = "{\"begin\":{\"size\":1721904,\"md5\":\"0123456789ABCDEF0123456789abcdef\"}}";
  CHECK(proto_parse_update(b, strlen(b), &c, &err));
  CHECK(c.kind == UPD_BEGIN && c.size == 1721904 && !strcmp(c.md5, "0123456789abcdef0123456789abcdef"));
  const char *nomd5 = "{\"begin\":{\"size\":10}}";
  CHECK(proto_parse_update(nomd5, strlen(nomd5), &c, &err) && c.md5[0] == '\0');
  CHECK(proto_parse_update("{\"end\":true}", 12, &c, &err) && c.kind == UPD_END);
  CHECK(proto_parse_update("{\"abort\":true}", 14, &c, &err) && c.kind == UPD_ABORT);
  const char *badmd5 = "{\"begin\":{\"size\":10,\"md5\":\"xyz\"}}";
  CHECK(!proto_parse_update(badmd5, strlen(badmd5), &c, &err) && !strcmp(err, "bad md5"));
  const char *nosize = "{\"begin\":{}}";
  CHECK(!proto_parse_update(nosize, strlen(nosize), &c, &err) && !strcmp(err, "bad size"));
  CHECK(!proto_parse_update("{}", 2, &c, &err));
}

static void test_json_escape() {
  char out[32];
  proto_json_escape(out, sizeof out, "a\"b\\c\n\x01");
  CHECK(!strcmp(out, "a\\\"b\\\\c\\u000a\\u0001"));
  char small[4];
  proto_json_escape(small, sizeof small, "\"\"\"");
  CHECK(!strcmp(small, "\\\""));  // never splits an escape
}

int main() {
  test_header_encoding();
  test_decodes_one_frame_and_stops_after_it();
  test_any_chunking_gives_the_same_payload();
  test_skips_log_text_and_false_magic();
  test_bad_headers_are_dropped_then_resync();
  test_rejected_frame_is_skipped_but_ended();
  test_stalled_frame_times_out_and_next_request_decodes();
  test_need_never_reads_past_a_frame();
  test_max_payload_streams_without_buffering();
  test_pages_csv_normalizes();
  test_default_pages();
  test_settings_full_update();
  test_settings_partial_and_merge();
  test_settings_rejects_bad_values();
  test_settings_json_round_trips();
  test_image_header();
  test_update_commands();
  test_json_escape();
  if (g_fail) {
    std::printf("%d link check(s) FAILED\n", g_fail);
    return 1;
  }
  std::printf("all link tests passed\n");
  return 0;
}
