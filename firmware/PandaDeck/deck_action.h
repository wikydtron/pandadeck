/* Deck button action grammar -> HID codes. Pure (host-tested).
 *
 *   keys:<K>[+<K>...]   chord, e.g. keys:CTRL+SHIFT+ESC, keys:GUI+LEFT, keys:F13
 *   media:<name>        playpause | next | prev | mute | volup | voldown | stop
 *   agent:<cmd>         forwarded to the PC agent, e.g. agent:launch:terminal
 *
 * Key codes use the Arduino USBHIDKeyboard encoding (modifiers 0x80-0x87,
 * specials 0xB0+, printable ASCII as-is) so they go straight to press(). */
#pragma once
#include <stdint.h>

#define DECK_MAX_CHORD 6
#define DECK_AGENT_MAX 40

enum DeckActionType : uint8_t { DECK_NONE = 0, DECK_KEYS, DECK_MEDIA, DECK_AGENT };

struct DeckAction {
  DeckActionType type;
  uint8_t keys[DECK_MAX_CHORD];
  uint8_t n_keys;
  uint16_t usage;                /* consumer-control usage id */
  char agent[DECK_AGENT_MAX];
};

/* Returns false (type DECK_NONE) on anything it does not understand. */
bool deck_parse_action(const char *s, DeckAction *out);
