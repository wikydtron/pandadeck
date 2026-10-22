#include "deck_action.h"

#include <ctype.h>
#include <string.h>

struct NamedCode {
  const char *name;
  uint16_t code;
};

/* Arduino USBHIDKeyboard codes. */
static const NamedCode KEYS[] = {
    {"CTRL", 0x80},  {"SHIFT", 0x81}, {"ALT", 0x82},    {"GUI", 0x83},   {"WIN", 0x83},
    {"RCTRL", 0x84}, {"RSHIFT", 0x85}, {"RALT", 0x86},  {"RGUI", 0x87},
    {"UP", 0xDA},    {"DOWN", 0xD9},  {"LEFT", 0xD8},   {"RIGHT", 0xD7},
    {"ENTER", 0xB0}, {"RETURN", 0xB0}, {"ESC", 0xB1},   {"BACKSPACE", 0xB2},
    {"TAB", 0xB3},   {"SPACE", ' '},  {"INSERT", 0xD1}, {"DELETE", 0xD4}, {"DEL", 0xD4},
    {"HOME", 0xD2},  {"END", 0xD5},   {"PGUP", 0xD3},   {"PGDN", 0xD6},
    {"PRTSC", 0xCE}, {"MENU", 0xED},  {"CAPSLOCK", 0xC1},
    {"PERIOD", '.'}, {"COMMA", ','},  {"MINUS", '-'},   {"EQUAL", '='},  {"SLASH", '/'},
    {"SEMICOLON", ';'}, {"QUOTE", '\''}, {"BACKSLASH", '\\'}, {"GRAVE", '`'},
    {"LBRACKET", '['}, {"RBRACKET", ']'},
};

static const NamedCode MEDIA[] = {
    {"playpause", 0x00CD}, {"play", 0x00CD}, {"next", 0x00B5}, {"prev", 0x00B6},
    {"mute", 0x00E2},      {"volup", 0x00E9}, {"voldown", 0x00EA}, {"stop", 0x00B7},
};

static bool ieq(const char *a, const char *b) {
  for (; *a && *b; a++, b++)
    if (toupper((unsigned char)*a) != toupper((unsigned char)*b)) return false;
  return *a == *b;
}

/* One token of a chord: named key, F1-F24, or a single printable char. */
static bool key_code(const char *tok, uint8_t *code) {
  for (const NamedCode &k : KEYS) {
    if (ieq(tok, k.name)) {
      *code = (uint8_t)k.code;
      return true;
    }
  }
  if ((tok[0] == 'F' || tok[0] == 'f') && isdigit((unsigned char)tok[1])) {
    int n = 0;
    for (const char *p = tok + 1; *p; p++) {
      if (!isdigit((unsigned char)*p)) return false;
      n = n * 10 + (*p - '0');
    }
    if (n >= 1 && n <= 12) { *code = (uint8_t)(0xC2 + n - 1); return true; }
    if (n >= 13 && n <= 24) { *code = (uint8_t)(0xF0 + n - 13); return true; }
    return false;
  }
  if (tok[0] && !tok[1] && tok[0] > ' ' && tok[0] < 0x7F) {
    /* Lower-case letters: an upper-case 'S' would make the HID layer add a
     * hidden SHIFT. Explicit SHIFT is written as SHIFT+S. */
    *code = (uint8_t)tolower((unsigned char)tok[0]);
    return true;
  }
  return false;
}

static bool parse_keys(const char *spec, DeckAction *out) {
  char buf[48];
  strncpy(buf, spec, sizeof buf - 1);
  buf[sizeof buf - 1] = '\0';
  out->n_keys = 0;
  char *save = nullptr;
  for (char *tok = strtok_r(buf, "+", &save); tok; tok = strtok_r(nullptr, "+", &save)) {
    if (out->n_keys >= DECK_MAX_CHORD) return false;
    uint8_t c;
    if (!key_code(tok, &c)) return false;
    out->keys[out->n_keys++] = c;
  }
  return out->n_keys > 0;
}

bool deck_parse_action(const char *s, DeckAction *out) {
  memset(out, 0, sizeof *out);
  if (!s) return false;
  if (!strncmp(s, "keys:", 5)) {
    if (parse_keys(s + 5, out)) { out->type = DECK_KEYS; return true; }
  } else if (!strncmp(s, "media:", 6)) {
    for (const NamedCode &m : MEDIA) {
      if (ieq(s + 6, m.name)) {
        out->type = DECK_MEDIA;
        out->usage = m.code;
        return true;
      }
    }
  } else if (!strncmp(s, "agent:", 6) && s[6]) {
    strncpy(out->agent, s + 6, DECK_AGENT_MAX - 1);
    out->type = DECK_AGENT;
    return true;
  }
  memset(out, 0, sizeof *out);
  return false;
}
