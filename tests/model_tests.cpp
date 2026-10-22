// Host tests for the firmware's pure modules (model.cpp + deck_action.cpp).
// Build + run: tests/run_host_tests.sh
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>

#include "deck_action.h"
#include "model.h"

static int g_fail = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    if (!(cond)) {                                                      \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);       \
      g_fail++;                                                         \
    }                                                                   \
  } while (0)

static std::string read_file(const char *p) {
  std::ifstream f(p, std::ios::binary);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

static void test_parses_agent_fixture(const char *fixture) {
  std::string j = read_file(fixture);
  CHECK(!j.empty());
  static Model m;
  static DeckLayout d;
  CHECK(model_parse(j.data(), j.size(), &m, &d));
  CHECK(m.epoch == 1792523303);
  CHECK(m.n_sessions == 2);
  CHECK(!strcmp(m.sessions[0].proj, "PandaDeck"));
  CHECK(m.sessions[0].st == SESS_WORKING);
  CHECK(m.sessions[1].st == SESS_WAITING);
  CHECK(m.sessions[0].src == SRC_CLAUDE && m.sessions[1].src == SRC_CODEX);
  CHECK(m.sessions[0].dur_s == 720);
  CHECK(model_count_state(m, SESS_WORKING) == 1);
  CHECK(m.n_perms == 1);
  CHECK(!strcmp(m.perms[0].tool, "Bash"));
  CHECK(strstr(m.perms[0].detail, "rm -rf build\n") == m.perms[0].detail);
  CHECK(m.usage.ok && std::fabs(m.usage.week - 68.0f) < 0.01f && m.usage.s5h_reset_s == 6696);
  CHECK(m.pc.ok && m.pc.mic_muted && m.pc.vol == 40 && std::fabs(m.pc.cpu_t + 1) < 0.01f);
  CHECK(m.lab.ok && m.lab.run == 12 && m.lab.n_bad == 1 && m.lab.n_top == 1);
  CHECK(!strcmp(m.lab.bad[0].health, "unhealthy"));
  CHECK(m.has_deck && d.n_page == 1 && d.rev == 268435457u);
  CHECK(d.page[0].color == 0x34D399 && d.page[0].n_btn == 2);
  CHECK(d.page[0].btn[1].color == 0xC084FC && !strcmp(d.page[0].btn[0].action, "keys:GUI+D"));
}

static void test_missing_sections_are_not_ok() {
  static Model m;
  const char *j = "{\"sessions\":[]}";
  CHECK(model_parse(j, strlen(j), &m, nullptr));
  CHECK(m.n_sessions == 0 && m.n_perms == 0);
  CHECK(!m.pc.ok && !m.lab.ok && !m.usage.ok && !m.has_deck);
}

static void test_bad_json_leaves_model_untouched() {
  static Model m;
  m.n_sessions = 3;
  CHECK(!model_parse("{nope", 5, &m, nullptr));
  CHECK(m.n_sessions == 3);
}

static void test_overlong_strings_truncate_safely() {
  std::string big(2000, 'x');
  std::string j = "{\"perms\":[{\"id\":\"1\",\"detail\":\"" + big + "\"}],\"sessions\":[{\"proj\":\"" + big + "\"}]}";
  static Model m;
  CHECK(model_parse(j.data(), j.size(), &m, nullptr));
  CHECK(strlen(m.perms[0].detail) == sizeof(m.perms[0].detail) - 1);
  CHECK(strlen(m.sessions[0].proj) == sizeof(m.sessions[0].proj) - 1);
}

static void test_caps_session_and_perm_counts() {
  std::string j = "{\"sessions\":[";
  for (int i = 0; i < 20; i++) j += std::string(i ? "," : "") + "{\"id\":\"s\",\"st\":\"work\"}";
  j += "],\"perms\":[";
  for (int i = 0; i < 9; i++) j += std::string(i ? "," : "") + "{\"id\":\"p\"}";
  j += "]}";
  static Model m;
  CHECK(model_parse(j.data(), j.size(), &m, nullptr));
  CHECK(m.n_sessions == MODEL_MAX_SESSIONS && m.n_perms == MODEL_MAX_PERMS);
}

static void test_default_deck_parses() {
  static Model m;
  static DeckLayout d;
  const char *j = model_default_deck_json();
  CHECK(model_parse(j, strlen(j), &m, &d));
  CHECK(d.n_page == 4 && d.rev == 1);
  for (int p = 0; p < d.n_page; p++) {
    CHECK(d.page[p].n_btn == 12);
    for (int b = 0; b < d.page[p].n_btn; b++) {
      DeckAction a;
      bool ok = deck_parse_action(d.page[p].btn[b].action, &a);
      if (!ok) std::printf("  unparsable default action: %s\n", d.page[p].btn[b].action);
      CHECK(ok);
    }
  }
}

static void test_deck_actions() {
  DeckAction a;
  CHECK(deck_parse_action("keys:CTRL+SHIFT+ESC", &a) && a.type == DECK_KEYS && a.n_keys == 3);
  CHECK(a.keys[0] == 0x80 && a.keys[1] == 0x81 && a.keys[2] == 0xB1);
  CHECK(deck_parse_action("keys:GUI+SHIFT+S", &a) && a.keys[2] == 's');  // lower-case: no hidden shift
  CHECK(deck_parse_action("keys:F13", &a) && a.keys[0] == 0xF0);
  CHECK(deck_parse_action("keys:F24", &a) && a.keys[0] == 0xFB);
  CHECK(deck_parse_action("keys:f1", &a) && a.keys[0] == 0xC2);
  CHECK(deck_parse_action("keys:gui+period", &a) && a.keys[1] == '.');
  CHECK(deck_parse_action("media:playpause", &a) && a.type == DECK_MEDIA && a.usage == 0xCD);
  CHECK(deck_parse_action("media:VolUp", &a) && a.usage == 0xE9);
  CHECK(deck_parse_action("agent:launch:terminal", &a) && a.type == DECK_AGENT);
  CHECK(!strcmp(a.agent, "launch:terminal"));
  CHECK(!deck_parse_action("keys:F25", &a) && a.type == DECK_NONE);
  CHECK(!deck_parse_action("keys:CTRL+BOGUS", &a));
  CHECK(!deck_parse_action("keys:", &a));
  CHECK(!deck_parse_action("media:explode", &a));
  CHECK(!deck_parse_action("agent:", &a));
  CHECK(!deck_parse_action("nonsense", &a));
  CHECK(!deck_parse_action(nullptr, &a));
  CHECK(!deck_parse_action("keys:A+B+C+D+E+F+G", &a));  // > DECK_MAX_CHORD
}

static void test_media_parses_and_defaults() {
  static Model m;
  const char *j =
      "{\"media\":{\"ok\":true,\"app\":\"Spotify\",\"title\":\"Song\",\"artist\":\"Band\","
      "\"album\":\"Album\",\"playing\":true,\"pos\":61,\"dur\":233,\"shuffle\":true,"
      "\"repeat\":2,\"art\":\"2afdb03a\",\"tint\":\"#D39568\"}}";
  CHECK(model_parse(j, strlen(j), &m, nullptr));
  CHECK(m.media.ok && m.media.playing);
  CHECK(!strcmp(m.media.title, "Song") && !strcmp(m.media.artist, "Band"));
  CHECK(!strcmp(m.media.art, "2afdb03a"));
  CHECK(m.media.tint == 0xD39568);
  CHECK(m.media.app_color == 0);  /* absent -> 0 = use the tint */
  const char *sp = "{\"media\":{\"ok\":true,\"app\":\"Spotify\",\"app_color\":\"#1DB954\"}}";
  CHECK(model_parse(sp, strlen(sp), &m, nullptr));
  CHECK(m.media.app_color == 0x1DB954);
  CHECK(m.media.liked == -1);  /* not reported -> unavailable */
  CHECK(model_parse(j, strlen(j), &m, nullptr));
  CHECK(m.media.pos_s == 61 && m.media.dur_s == 233);
  CHECK(m.media.shuffle == 1 && m.media.repeat == 2);

  const char *none = "{\"media\":{\"ok\":false}}";
  CHECK(model_parse(none, strlen(none), &m, nullptr));
  CHECK(!m.media.ok && !m.media.playing && m.media.title[0] == '\0');
  CHECK(m.media.shuffle == -1 && m.media.repeat == -1);

  const char *unknown = "{\"media\":{\"ok\":true,\"title\":\"x\",\"shuffle\":-1,\"pos\":-5}}";
  CHECK(model_parse(unknown, strlen(unknown), &m, nullptr));
  CHECK(m.media.ok && m.media.shuffle == -1 && m.media.repeat == -1 && m.media.pos_s == 0);
}


static void test_lab_and_pc_names() {
  static Model m;
  const char *j = "{\"pc\":{\"name\":\"studio-pc\",\"cpu\":12,\"cpu_model\":\"Ryzen 5 7600\",\"gpu_model\":\"RTX 4060\"},"
                  "\"lab\":{\"name\":\"nas\",\"cpu\":3,\"cpu_model\":\"N100\",\"gpu_model\":\"\"}}";
  CHECK(model_parse(j, strlen(j), &m, nullptr));
  CHECK(m.pc.ok && !strcmp(m.pc.name, "studio-pc") && m.lab.ok && !strcmp(m.lab.name, "nas"));
  CHECK(!strcmp(m.pc.cpu_model, "Ryzen 5 7600") && !strcmp(m.pc.gpu_model, "RTX 4060"));
  CHECK(!strcmp(m.lab.cpu_model, "N100") && m.lab.gpu_model[0] == '\0');
  const char *unnamed = "{\"lab\":{\"cpu\":3},\"pc\":{\"cpu_model\":\"AMD Ryzen Threadripper PRO 7995WX 96-Cores\"}}";
  CHECK(model_parse(unnamed, strlen(unnamed), &m, nullptr));
  CHECK(m.lab.ok && !strcmp(m.lab.name, "Server") && m.lab.cpu_model[0] == '\0');
  CHECK(m.pc.ok && strlen(m.pc.cpu_model) == sizeof(m.pc.cpu_model) - 1 && m.pc.name[0] == '\0');  // truncated
  const char *none = "{}";
  CHECK(model_parse(none, 2, &m, nullptr));
  CHECK(!m.lab.ok && m.lab.name[0] == '\0');
}

static void test_deck_style_parses() {
  static Model m;
  static DeckLayout d;
  const char *j = "{\"deck\":{\"rev\":5,\"style\":\"icons\",\"labels\":false,\"pages\":[{\"name\":\"A\",\"buttons\":[]}]}}";
  CHECK(model_parse(j, strlen(j), &m, &d));
  CHECK(d.style == DECK_ICONS && !d.labels);
  const char *k = "{\"deck\":{\"rev\":6,\"pages\":[{\"name\":\"A\",\"buttons\":[]}]}}";
  CHECK(model_parse(k, strlen(k), &m, &d));
  CHECK(d.style == DECK_CLASSIC && d.labels);  /* defaults: unchanged look */
}

int main(int argc, char **argv) {
  test_parses_agent_fixture(argc > 1 ? argv[1] : "tests/fixtures/snapshot.json");
  test_missing_sections_are_not_ok();
  test_bad_json_leaves_model_untouched();
  test_overlong_strings_truncate_safely();
  test_caps_session_and_perm_counts();
  test_default_deck_parses();
  test_deck_actions();
  test_media_parses_and_defaults();
  test_deck_style_parses();
  test_lab_and_pc_names();
  if (g_fail) {
    std::printf("%d check(s) FAILED\n", g_fail);
    return 1;
  }
  std::printf("all host tests passed\n");
  return 0;
}
