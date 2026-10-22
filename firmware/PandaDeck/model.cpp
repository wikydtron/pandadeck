#include "model.h"

#include <ArduinoJson.h>
#include <string.h>

static void copy_str(char *dst, size_t cap, JsonVariantConst v) {
  const char *s = v.is<const char *>() ? v.as<const char *>() : nullptr;
  if (!s) {
    dst[0] = '\0';
    return;
  }
  strncpy(dst, s, cap - 1);
  dst[cap - 1] = '\0';
}

static float num_or(JsonVariantConst v, float dflt) {
  if (v.is<float>() || v.is<int>() || v.is<long>()) return v.as<float>();
  return dflt;
}

static int32_t int_or(JsonVariantConst v, int32_t dflt) {
  if (v.is<int>() || v.is<long>() || v.is<float>()) return (int32_t)v.as<long>();
  return dflt;
}

static uint8_t parse_state(const char *s) {
  if (!s) return SESS_IDLE;
  if (!strcmp(s, "work")) return SESS_WORKING;
  if (!strcmp(s, "wait")) return SESS_WAITING;
  if (!strcmp(s, "perm")) return SESS_PERMISSION;
  return SESS_IDLE;
}

static uint8_t parse_src(const char *s) { return (s && !strcmp(s, "codex")) ? SRC_CODEX : SRC_CLAUDE; }

static uint32_t parse_color(JsonVariantConst v) {
  if (v.is<unsigned long>() || v.is<int>()) return (uint32_t)v.as<unsigned long>();
  const char *s = v.is<const char *>() ? v.as<const char *>() : nullptr;
  if (!s) return 0;
  if (*s == '#') s++;
  uint32_t c = 0;
  for (int i = 0; i < 6 && s[i]; i++) {
    char ch = s[i];
    uint32_t d = (ch >= '0' && ch <= '9') ? ch - '0'
               : (ch >= 'a' && ch <= 'f') ? ch - 'a' + 10
               : (ch >= 'A' && ch <= 'F') ? ch - 'A' + 10 : 0;
    c = (c << 4) | d;
  }
  return c;
}

static void parse_sessions(JsonArrayConst arr, Model *m) {
  m->n_sessions = 0;
  for (JsonObjectConst o : arr) {
    if (m->n_sessions >= MODEL_MAX_SESSIONS) break;
    SessionInfo &s = m->sessions[m->n_sessions++];
    copy_str(s.id, sizeof s.id, o["id"]);
    copy_str(s.proj, sizeof s.proj, o["proj"]);
    copy_str(s.title, sizeof s.title, o["title"]);
    copy_str(s.act, sizeof s.act, o["act"]);
    copy_str(s.host, sizeof s.host, o["host"]);
    copy_str(s.model, sizeof s.model, o["model"]);
    s.st = parse_state(o["st"] | "idle");
    s.src = parse_src(o["src"] | "claude");
    s.age_s = int_or(o["age"], 0);
    s.dur_s = int_or(o["dur"], 0);
  }
}

static void parse_perms(JsonArrayConst arr, Model *m) {
  m->n_perms = 0;
  for (JsonObjectConst o : arr) {
    if (m->n_perms >= MODEL_MAX_PERMS) break;
    PermInfo &p = m->perms[m->n_perms++];
    copy_str(p.id, sizeof p.id, o["id"]);
    copy_str(p.sid, sizeof p.sid, o["sid"]);
    copy_str(p.proj, sizeof p.proj, o["proj"]);
    copy_str(p.host, sizeof p.host, o["host"]);
    copy_str(p.tool, sizeof p.tool, o["tool"]);
    copy_str(p.detail, sizeof p.detail, o["detail"]);
    p.src = parse_src(o["src"] | "claude");
    p.age_s = int_or(o["age"], 0);
  }
}

static void parse_usage(JsonObjectConst o, UsageInfo *u) {
  memset(u, 0, sizeof *u);
  if (o.isNull()) return;
  u->ok = o["ok"] | true;
  u->s5h = num_or(o["s5h"], -1);
  u->s5h_reset_s = int_or(o["s5h_reset"], -1);
  u->week = num_or(o["week"], -1);
  u->week_reset_s = int_or(o["week_reset"], -1);
  u->codex_week = num_or(o["codex_week"], -1);
  u->codex_reset_s = int_or(o["codex_reset"], -1);
}

static void parse_pc(JsonObjectConst o, PcInfo *p) {
  memset(p, 0, sizeof *p);
  if (o.isNull()) return;
  p->ok = true;
  copy_str(p->name, sizeof p->name, o["name"]);
  copy_str(p->cpu_model, sizeof p->cpu_model, o["cpu_model"]);
  copy_str(p->gpu_model, sizeof p->gpu_model, o["gpu_model"]);
  p->cpu = num_or(o["cpu"], -1);
  p->ram = num_or(o["ram"], -1);
  p->gpu = num_or(o["gpu"], -1);
  p->gpu_t = num_or(o["gpu_t"], -1);
  p->gpu_w = num_or(o["gpu_w"], -1);
  p->vram = num_or(o["vram"], -1);
  p->cpu_t = num_or(o["cpu_t"], -1);
  p->disk = num_or(o["disk"], -1);
  p->down_mbps = num_or(o["down"], -1);
  p->up_mbps = num_or(o["up"], -1);
  p->mic_muted = o["mic_muted"] | false;
  p->vol = (int8_t)int_or(o["vol"], -1);
}

static void parse_lab_items(JsonArrayConst arr, LabItem *items, uint8_t *n) {
  *n = 0;
  for (JsonObjectConst o : arr) {
    if (*n >= MODEL_MAX_LAB_ITEMS) break;
    LabItem &it = items[(*n)++];
    copy_str(it.id, sizeof it.id, o["id"]);
    copy_str(it.name, sizeof it.name, o["name"]);
    copy_str(it.state, sizeof it.state, o["state"]);
    copy_str(it.health, sizeof it.health, o["health"]);
    it.cpu = num_or(o["cpu"], -1);
    it.mem = num_or(o["mem"], -1);
  }
}

static void parse_lab(JsonObjectConst o, LabInfo *l) {
  memset(l, 0, sizeof *l);
  if (o.isNull()) return;
  l->ok = o["ok"] | true;
  copy_str(l->name, sizeof l->name, o["name"]);
  if (!l->name[0]) strcpy(l->name, "Server");
  copy_str(l->cpu_model, sizeof l->cpu_model, o["cpu_model"]);
  copy_str(l->gpu_model, sizeof l->gpu_model, o["gpu_model"]);
  l->cpu = num_or(o["cpu"], -1);
  l->ram = num_or(o["ram"], -1);
  l->temp = num_or(o["temp"], -1);
  l->gpu = num_or(o["gpu"], -1);
  l->gpu_t = num_or(o["gpu_t"], -1);
  l->disk = num_or(o["disk"], -1);
  l->backup_h = num_or(o["backup_h"], -1);
  l->run = (int16_t)int_or(o["run"], -1);
  l->total = (int16_t)int_or(o["total"], -1);
  l->healthy = (int16_t)int_or(o["healthy"], -1);
  parse_lab_items(o["bad"], l->bad, &l->n_bad);
  parse_lab_items(o["top"], l->top, &l->n_top);
}

static void parse_media(JsonObjectConst o, MediaInfo *m) {
  memset(m, 0, sizeof *m);
  m->shuffle = m->repeat = m->liked = -1;
  if (o.isNull() || !(o["ok"] | false)) return;
  m->ok = true;
  m->playing = o["playing"] | false;
  copy_str(m->app, sizeof m->app, o["app"]);
  copy_str(m->title, sizeof m->title, o["title"]);
  copy_str(m->artist, sizeof m->artist, o["artist"]);
  copy_str(m->album, sizeof m->album, o["album"]);
  copy_str(m->art, sizeof m->art, o["art"]);
  m->tint = parse_color(o["tint"]);
  m->app_color = parse_color(o["app_color"]);
  m->liked = (int8_t)int_or(o["liked"], -1);
  m->pos_s = int_or(o["pos"], 0);
  m->dur_s = int_or(o["dur"], 0);
  if (m->pos_s < 0) m->pos_s = 0;
  if (m->dur_s < 0) m->dur_s = 0;
  JsonVariantConst sh = o["shuffle"];
  m->shuffle = sh.is<bool>() ? (sh.as<bool>() ? 1 : 0) : (int8_t)int_or(sh, -1);
  m->repeat = (int8_t)int_or(o["repeat"], -1);
}


static void parse_deck(JsonObjectConst o, DeckLayout *d) {
  d->rev = (uint32_t)int_or(o["rev"], (int32_t)d->rev + 1);
  const char *style = o["style"] | "classic";
  d->style = !strcmp(style, "icons") ? DECK_ICONS : DECK_CLASSIC;
  d->labels = o["labels"] | true;
  d->n_page = 0;
  for (JsonObjectConst po : o["pages"].as<JsonArrayConst>()) {
    if (d->n_page >= MODEL_MAX_DECK_PAGES) break;
    DeckPage &p = d->page[d->n_page++];
    copy_str(p.name, sizeof p.name, po["name"]);
    p.color = parse_color(po["color"]);
    p.cols = (po["cols"] | 4) >= 6 ? 6 : 4;
    p.n_btn = 0;
    for (JsonObjectConst bo : po["buttons"].as<JsonArrayConst>()) {
      if (p.n_btn >= MODEL_MAX_DECK_BUTTONS) break;
      DeckButton &b = p.btn[p.n_btn++];
      copy_str(b.label, sizeof b.label, bo["label"]);
      copy_str(b.icon, sizeof b.icon, bo["icon"]);
      copy_str(b.img, sizeof b.img, bo["img"]);
      copy_str(b.action, sizeof b.action, bo["action"]);
      b.color = parse_color(bo["color"]);
    }
  }
}

bool model_parse(const char *json, size_t len, Model *out, DeckLayout *deck) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, json, len);
  if (err || !doc.is<JsonObject>()) return false;
  JsonObjectConst root = doc.as<JsonObjectConst>();

  parse_sessions(root["sessions"].as<JsonArrayConst>(), out);
  parse_perms(root["perms"].as<JsonArrayConst>(), out);
  parse_usage(root["usage"].as<JsonObjectConst>(), &out->usage);
  parse_pc(root["pc"].as<JsonObjectConst>(), &out->pc);
  parse_lab(root["lab"].as<JsonObjectConst>(), &out->lab);
  parse_media(root["media"].as<JsonObjectConst>(), &out->media);
  out->epoch = root["t"] | (int64_t)0;
  out->ack = root["ack"] | 0u;
  out->ack_boot = root["boot"] | 0u;
  out->has_deck = false;
  if (deck && root["deck"].is<JsonObjectConst>()) {
    parse_deck(root["deck"].as<JsonObjectConst>(), deck);
    out->has_deck = true;
  }
  return true;
}

int model_count_state(const Model &m, SessionState st) {
  int n = 0;
  for (int i = 0; i < m.n_sessions; i++)
    if (m.sessions[i].st == st) n++;
  return n;
}

/* Default deck. Same grammar the agent can override via "deck" in a sync.
 * F13-F24 are deliberately unbound in Windows: map them in OBS/Discord/AHK. */
const char *model_default_deck_json() {
  return R"JSON({"deck":{"rev":1,"pages":[
 {"name":"MEDIA","color":"#C084FC","buttons":[
  {"label":"Prev","icon":"prev","action":"media:prev"},
  {"label":"Play","icon":"play","action":"media:playpause"},
  {"label":"Next","icon":"next","action":"media:next"},
  {"label":"Mute","icon":"mute","action":"media:mute"},
  {"label":"Vol -","icon":"vol_down","action":"media:voldown"},
  {"label":"Vol +","icon":"vol_up","action":"media:volup"},
  {"label":"Mic","icon":"mic","action":"agent:mic"},
  {"label":"Spotify","icon":"audio","action":"agent:launch:spotify"},
  {"label":"Snip","icon":"image","action":"keys:GUI+SHIFT+S"},
  {"label":"Desktop","icon":"home","action":"keys:GUI+D"},
  {"label":"Task Mgr","icon":"list","action":"keys:CTRL+SHIFT+ESC"},
  {"label":"Lock","icon":"lock","action":"keys:GUI+L"}]},
 {"name":"WINDOWS","color":"#60A5FA","buttons":[
  {"label":"Snap L","icon":"left","action":"keys:GUI+LEFT"},
  {"label":"Snap R","icon":"right","action":"keys:GUI+RIGHT"},
  {"label":"Maximize","icon":"up","action":"keys:GUI+UP"},
  {"label":"Minimize","icon":"down","action":"keys:GUI+DOWN"},
  {"label":"Task View","icon":"apps","action":"keys:GUI+TAB"},
  {"label":"Explorer","icon":"folder","action":"keys:GUI+E"},
  {"label":"Clipboard","icon":"paste","action":"keys:GUI+V"},
  {"label":"Emoji","icon":"smile","action":"keys:GUI+PERIOD"},
  {"label":"Settings","icon":"settings","action":"keys:GUI+I"},
  {"label":"Run","icon":"play","action":"keys:GUI+R"},
  {"label":"Alt-Tab","icon":"loop","action":"keys:ALT+TAB"},
  {"label":"Close","icon":"close","action":"keys:ALT+F4"}]},
 {"name":"DEV","color":"#D97757","buttons":[
  {"label":"Terminal","icon":"terminal","action":"agent:launch:terminal"},
  {"label":"Claude","icon":"spark","action":"agent:launch:claude"},
  {"label":"VS Code","icon":"edit","action":"agent:launch:vscode"},
  {"label":"Browser","icon":"web","action":"agent:launch:browser"},
  {"label":"Copy","icon":"copy","action":"keys:CTRL+C"},
  {"label":"Paste","icon":"paste","action":"keys:CTRL+V"},
  {"label":"Undo","icon":"undo","action":"keys:CTRL+Z"},
  {"label":"Redo","icon":"redo","action":"keys:CTRL+Y"},
  {"label":"Save","icon":"save","action":"keys:CTRL+S"},
  {"label":"Find","icon":"search","action":"keys:CTRL+F"},
  {"label":"Esc","icon":"close","action":"keys:ESC"},
  {"label":"Enter","icon":"enter","action":"keys:ENTER"}]},
 {"name":"MACROS","color":"#34D399","buttons":[
  {"label":"F13","icon":"bolt","action":"keys:F13"},
  {"label":"F14","icon":"bolt","action":"keys:F14"},
  {"label":"F15","icon":"bolt","action":"keys:F15"},
  {"label":"F16","icon":"bolt","action":"keys:F16"},
  {"label":"F17","icon":"bolt","action":"keys:F17"},
  {"label":"F18","icon":"bolt","action":"keys:F18"},
  {"label":"F19","icon":"bolt","action":"keys:F19"},
  {"label":"F20","icon":"bolt","action":"keys:F20"},
  {"label":"F21","icon":"bolt","action":"keys:F21"},
  {"label":"F22","icon":"bolt","action":"keys:F22"},
  {"label":"F23","icon":"bolt","action":"keys:F23"},
  {"label":"F24","icon":"bolt","action":"keys:F24"}]}
]}})JSON";
}
