#include "store.h"

#include <Arduino.h>
#include <esp_random.h>
#include <freertos/semphr.h>
#include <string.h>

#define STORE_MAX_EVENTS 16

/* Outbound taps are kept until the agent acknowledges their sequence number
 * in a later sync ("ack"), so a sync response lost on a stalled socket can't
 * silently drop an Approve/Deny. */
struct OutEvent {
  uint32_t seq;
  char kind[8];
  char id[24];
  char arg[48];
};

static SemaphoreHandle_t s_mu = nullptr;
static Model *s_model = nullptr;   /* PSRAM */
static Model *s_scratch = nullptr; /* parse target, swapped in on success */
static DeckLayout *s_deck = nullptr;
static DeckLayout *s_deck_tmp = nullptr;
static uint32_t s_version = 0;
static uint32_t s_last_sync_ms = 0;
static bool s_ever_synced = false;
static OutEvent s_events[STORE_MAX_EVENTS];
static uint8_t s_n_events = 0;
static uint32_t s_next_seq = 1;
static uint32_t s_boot_id = 0;

struct Lock {
  Lock() { xSemaphoreTake(s_mu, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(s_mu); }
};

void store_init() {
  s_mu = xSemaphoreCreateMutex();
  s_model = (Model *)ps_calloc(1, sizeof(Model));
  s_scratch = (Model *)ps_calloc(1, sizeof(Model));
  s_deck = (DeckLayout *)ps_calloc(1, sizeof(DeckLayout));
  s_deck_tmp = (DeckLayout *)ps_calloc(1, sizeof(DeckLayout));
  s_boot_id = esp_random() | 1u;
  const char *dflt = model_default_deck_json();
  model_parse(dflt, strlen(dflt), s_scratch, s_deck);
  memset(s_scratch, 0, sizeof(Model));
}

uint32_t store_boot_id() { return s_boot_id; }

static void ack_locked(uint32_t ack) {
  uint8_t keep = 0;
  for (uint8_t i = 0; i < s_n_events; i++)
    if (s_events[i].seq > ack) s_events[keep++] = s_events[i];
  s_n_events = keep;
}

bool store_apply_sync(const char *json, size_t len, int64_t *epoch) {
  /* Parse outside the lock into scratch: only the link task calls this. */
  {
    Lock l;
    *s_deck_tmp = *s_deck;
  }
  if (!model_parse(json, len, s_scratch, s_deck_tmp)) return false;
  if (epoch) *epoch = s_scratch->epoch;
  Lock l;
  Model *t = s_model;
  s_model = s_scratch;
  s_scratch = t;
  if (s_model->has_deck) *s_deck = *s_deck_tmp;
  if (s_model->ack_boot == s_boot_id) ack_locked(s_model->ack);
  s_version++;
  s_last_sync_ms = millis();
  s_ever_synced = true;
  return true;
}

static size_t json_escape(char *dst, size_t cap, const char *s) {
  size_t n = 0;
  for (; *s && n + 2 < cap; s++) {
    char c = *s;
    if (c == '"' || c == '\\') {
      dst[n++] = '\\';
      dst[n++] = c;
    } else if ((unsigned char)c >= 0x20) {
      dst[n++] = c;
    }
  }
  dst[n] = '\0';
  return n;
}

size_t store_drain_events(char *buf, size_t cap) {
  Lock l;
  if (cap < 3) return 0;
  size_t n = 0;
  buf[n++] = '[';
  for (uint8_t i = 0; i < s_n_events; i++) {
    char k[16], id[48], arg[96], row[200];
    json_escape(k, sizeof k, s_events[i].kind);
    json_escape(id, sizeof id, s_events[i].id);
    json_escape(arg, sizeof arg, s_events[i].arg);
    int r = snprintf(row, sizeof row, "%s{\"seq\":%lu,\"k\":\"%s\",\"id\":\"%s\",\"a\":\"%s\"}", i ? "," : "",
                     (unsigned long)s_events[i].seq, k, id, arg);
    if (r < 0 || (size_t)r >= sizeof row || n + (size_t)r + 2 > cap) break; /* rest goes next sync */
    memcpy(buf + n, row, (size_t)r);
    n += (size_t)r;
  }
  buf[n++] = ']';
  buf[n] = '\0';
  return n;
}

bool store_snapshot(Model *out, uint32_t *seen_version) {
  Lock l;
  if (*seen_version == s_version) return false;
  *out = *s_model;
  *seen_version = s_version;
  return true;
}

bool store_deck(DeckLayout *out, uint32_t *seen_rev) {
  Lock l;
  if (*seen_rev == s_deck->rev && *seen_rev != 0) return false;
  *out = *s_deck;
  *seen_rev = s_deck->rev;
  return true;
}

uint32_t store_ms_since_sync() {
  Lock l;
  return s_ever_synced ? millis() - s_last_sync_ms : UINT32_MAX;
}

void store_push_event(const char *kind, const char *id, const char *arg) {
  Lock l;
  if (s_n_events >= STORE_MAX_EVENTS) {
    /* Drop the oldest: a stale tap matters less than the newest one. */
    memmove(&s_events[0], &s_events[1], sizeof(OutEvent) * (STORE_MAX_EVENTS - 1));
    s_n_events--;
  }
  OutEvent &e = s_events[s_n_events++];
  e.seq = s_next_seq++;
  strncpy(e.kind, kind ? kind : "", sizeof e.kind - 1);
  e.kind[sizeof e.kind - 1] = '\0';
  strncpy(e.id, id ? id : "", sizeof e.id - 1);
  e.id[sizeof e.id - 1] = '\0';
  strncpy(e.arg, arg ? arg : "", sizeof e.arg - 1);
  e.arg[sizeof e.arg - 1] = '\0';
}
