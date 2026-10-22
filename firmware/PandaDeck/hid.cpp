#include "hid.h"

#include <Arduino.h>
#include <USB.h>
#include <USBHIDConsumerControl.h>
#include <USBHIDKeyboard.h>
#include <esp_mac.h>
#include <esp_private/startup_internal.h>

static USBHIDKeyboard s_kb;
static USBHIDConsumerControl s_cc;

/* USB descriptor strings. With CDC-on-boot, Arduino's app_main starts USB
 * before setup() runs, so setting them in hid_init() would be too late. A
 * secondary-stage system init function runs after the C++ constructors (the
 * USB object exists) and before app_main.
 * Serial "PandaDeck-<MAC>": Windows' serial APIs don't expose the product
 * string but do expose the serial number, so the app can recognise the
 * panel's CDC port without probing every Espressif device. */
ESP_SYSTEM_INIT_FN(pd_usb_strings, SECONDARY, BIT(0), 999) {
  static char serial[24];
  uint8_t m[6] = {};
  esp_efuse_mac_get_default(m);
  snprintf(serial, sizeof serial, "PandaDeck-%02X%02X%02X%02X%02X%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
  USB.productName("PandaDeck");
  USB.manufacturerName("PandaDeck");
  USB.serialNumber(serial);
  return ESP_OK;
}

void hid_init() {
  s_kb.begin();
  s_cc.begin();
  USB.begin(); /* no-op when CDC-on-boot already started it */
}

bool hid_ready() { return (bool)USB; }

void hid_fire(const DeckAction &a) {
  if (a.type == DECK_KEYS) {
    for (uint8_t i = 0; i < a.n_keys; i++) s_kb.press(a.keys[i]);
    delay(30);
    s_kb.releaseAll();
  } else if (a.type == DECK_MEDIA) {
    s_cc.press(a.usage);
    delay(30);
    s_cc.release();
  }
}
