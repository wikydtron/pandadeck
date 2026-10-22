#pragma once
#include <stdint.h>

/* Resets the controller (selects I2C address 0x5D via INT low during reset)
 * and probes 0x5D / 0x14. Returns false if nothing answers. */
bool gt911_init(int sda, int scl, int pin_int, int pin_rst);
/* Returns false on I2C error. On success: *pressed + last coordinates. */
bool gt911_read(int16_t *x, int16_t *y, bool *pressed);
/* Max X/Y the controller is configured for (0 if unknown). */
void gt911_resolution(uint16_t *w, uint16_t *h);
