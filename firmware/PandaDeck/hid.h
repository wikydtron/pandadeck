#pragma once
#include "deck_action.h"

/* USB HID (keyboard + consumer control) over the native USB on the USB-C
 * port. Must be called before USB.begin(). */
void hid_init();
/* True once the PC host has configured the device. */
bool hid_ready();
/* Fires a parsed keys:/media: action (tap: press then release). */
void hid_fire(const DeckAction &a);
