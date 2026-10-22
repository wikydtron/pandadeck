/* PandaDeck shared data model.
 *
 * The desktop app pushes a JSON snapshot (USB `S` frame or POST /api/sync);
 * model_parse() turns it into this fixed-size struct (no heap churn on the UI
 * side). Pure C++ + ArduinoJson so it builds and is unit-tested on the host
 * (tests/). */
#pragma once
#include "features.h"

#include <stddef.h>
#include <stdint.h>

#define MODEL_MAX_SESSIONS 8
#define MODEL_MAX_PERMS 4
#define MODEL_MAX_LAB_ITEMS 6
#define MODEL_MAX_DECK_PAGES 6
#define MODEL_MAX_DECK_BUTTONS 18

enum SessionState : uint8_t {
  SESS_IDLE = 0,  /* open, nothing happening */
  SESS_WORKING,   /* Claude is generating / running tools */
  SESS_WAITING,   /* finished a turn or idle prompt: your move */
  SESS_PERMISSION /* blocked on a permission prompt */
};

enum AgentSrc : uint8_t { SRC_CLAUDE = 0, SRC_CODEX = 1 };

struct SessionInfo {
  char id[24];
  char proj[40];
  char title[80];
  char act[120];   /* "Bash: npm test" / last prompt / message */
  char host[20];
  char model[24];
  uint8_t st;      /* SessionState */
  uint8_t src;     /* AgentSrc */
  int32_t age_s;   /* seconds since last event */
  int32_t dur_s;   /* seconds since session start */
};

struct PermInfo {
  char id[24];
  char sid[24];
  char proj[40];
  char host[20];
  char tool[32];
  char detail[640];
  uint8_t src;     /* AgentSrc */
  int32_t age_s;
};

struct UsageInfo {
  bool ok;
  float s5h;        /* Claude 5-hour window, percent (-1 = unknown) */
  int32_t s5h_reset_s;
  float week;       /* Claude weekly, percent */
  int32_t week_reset_s;
  float codex_week; /* Codex weekly, percent (-1 = unknown) */
  int32_t codex_reset_s;
};

struct PcInfo {
  bool ok;
  char name[40];   /* the computer's name: HUD column title */
  char cpu_model[24]; /* "Ryzen 5 7600", "" = not shown */
  char gpu_model[24]; /* "RTX 4060", "" = not shown */
  float cpu, ram, gpu, gpu_t, gpu_w, vram, cpu_t, disk; /* -1 = unknown */
  float down_mbps, up_mbps;
  bool mic_muted;
  int8_t vol;      /* 0..100, -1 unknown */
};

struct LabItem {
  char id[16];     /* short docker id (12 hex) */
  char name[32];
  char state[16];  /* running / exited / restarting */
  char health[16]; /* healthy / unhealthy / "" */
  float cpu, mem;
};

struct LabInfo {
  bool ok;
  char name[32];   /* server name: HUD column title ("Server" if absent) */
  char cpu_model[24];
  char gpu_model[24];
  float cpu, ram, temp, gpu, gpu_t, disk, backup_h; /* -1 = unknown */
  int16_t run, total, healthy;
  LabItem bad[MODEL_MAX_LAB_ITEMS];
  uint8_t n_bad;
  LabItem top[MODEL_MAX_LAB_ITEMS];
  uint8_t n_top;
};

/* Now playing on the PC (Windows media session: Spotify, browsers, ...). */
struct MediaInfo {
  bool ok;         /* a media session exists */
  bool playing;
  char app[20];    /* "Spotify" */
  char title[96];
  char artist[64];
  char album[64];
  char art[12];    /* cover hash; matches the tag of icon "art" once uploaded */
  uint32_t tint;   /* 0xRRGGBB accent from the cover, 0 = default */
  uint32_t app_color; /* the service's colour (Spotify green...), 0 = use tint */
  int8_t liked;      /* Spotify Liked Songs: 1 / 0, -1 = unavailable (not signed in) */
  int32_t pos_s, dur_s; /* dur 0 = unknown (no progress bar) */
  int8_t shuffle;  /* -1 not reported, 0 off, 1 on */
  int8_t repeat;   /* -1 not reported, 0 off, 1 track, 2 list */
};


/* Deck action string grammar (parsed at press time, see deck_action.h):
 *   keys:CTRL+SHIFT+ESC   media:playpause   agent:launch:terminal */
struct DeckButton {
  char label[20];
  char icon[16];   /* symbol name, see ui_icons */
  char img[16];    /* app icon key uploaded via /api/icon ("" = use `icon`) */
  uint32_t color;  /* 0xRRGGBB accent, 0 = page default */
  char action[48];
};

struct DeckPage {
  char name[12];
  uint32_t color;
  uint8_t cols;    /* 4 (12 big buttons) or 6 (18 app tiles) */
  DeckButton btn[MODEL_MAX_DECK_BUTTONS];
  uint8_t n_btn;
};

enum DeckStyle : uint8_t { DECK_CLASSIC = 0, DECK_ICONS = 1 };

struct DeckLayout {
  uint32_t rev;    /* changes when the agent sends a new layout */
  uint8_t style;   /* DeckStyle: cards with icon + name, or the icon IS the button */
  bool labels;     /* names under the icons (icons style; classic always shows them) */
  DeckPage page[MODEL_MAX_DECK_PAGES];
  uint8_t n_page;
};

struct Model {
  SessionInfo sessions[MODEL_MAX_SESSIONS];
  uint8_t n_sessions;
  PermInfo perms[MODEL_MAX_PERMS];
  uint8_t n_perms;
  UsageInfo usage;
  PcInfo pc;
  LabInfo lab;
  MediaInfo media;
  int64_t epoch;   /* agent's clock, 0 if absent */
  bool has_deck;   /* snapshot carried a deck layout */
  uint32_t ack;      /* highest outbound event seq the agent has processed */
  uint32_t ack_boot; /* panel boot id that ack refers to */
};

/* Parses a sync snapshot. Missing sections are marked !ok / zero-count.
 * If `deck` is non-null and the snapshot has a "deck" key, fills it.
 * Returns false only on malformed JSON (out is then left untouched). */
bool model_parse(const char *json, size_t len, Model *out, DeckLayout *deck);

/* Built-in default deck (used until the agent sends one). */
const char *model_default_deck_json();

/* Count helpers (pure). */
int model_count_state(const Model &m, SessionState st);
