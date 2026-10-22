/* Thread-safe hand-off between the link task (HTTP + USB: writes snapshots,
 * drains outbound events) and the LVGL loop (reads snapshots, queues taps). */
#pragma once
#include "model.h"

void store_init();

/* Link side: parse + publish a snapshot. Returns false on bad JSON.
 * epoch (optional) receives the snapshot's "t" (0 if absent). */
bool store_apply_sync(const char *json, size_t len, int64_t *epoch = nullptr);
/* Link side: serialize pending outbound events (with seq numbers) into buf as
 * a JSON array. Events stay queued until a later sync acks them. */
size_t store_drain_events(char *buf, size_t cap);
/* Random per boot: the agent echoes it with its ack so a rebooted panel
 * never mistakes an old ack for its own new sequence numbers. */
uint32_t store_boot_id();

/* UI side: copy the latest snapshot if it changed since *seen_version.
 * Returns true (and updates *seen_version) when out was refreshed. */
bool store_snapshot(Model *out, uint32_t *seen_version);
/* UI side: copy the deck layout if its revision changed. */
bool store_deck(DeckLayout *out, uint32_t *seen_rev);
/* Milliseconds since the agent last synced (UINT32_MAX if never). */
uint32_t store_ms_since_sync();

/* UI side: queue an outbound event. kind: "perm" (arg allow/deny),
 * "cmd" (arg = agent command). */
void store_push_event(const char *kind, const char *id, const char *arg);
