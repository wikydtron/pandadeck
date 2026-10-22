/* Minimal polling GT911 driver (I2C, 16-bit registers).
 * Adapted from an earlier ESP32 panel driver, plus the RST/INT address-select
 * sequence the Panda Touch needs (it wires both pins). */
#include "gt911.h"
#include <Arduino.h>
#include <Wire.h>

#include "pdlog.h"

static uint8_t s_addr = 0x5D;
static uint16_t s_w = 0, s_h = 0;
static bool s_last_pressed = false;
static int16_t s_last_x = 0, s_last_y = 0;

static bool wr_reg8(uint16_t reg, uint8_t val) {
  Wire.beginTransmission(s_addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool rd_regs(uint16_t reg, uint8_t *buf, size_t len) {
  Wire.beginTransmission(s_addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)s_addr, (int)len) != (int)len) return false;
  for (size_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

/* GT911 datasheet: INT held low while RST rises -> address 0x5D. */
static void reset_select_0x5d(int pin_int, int pin_rst) {
  if (pin_rst < 0) return;
  pinMode(pin_rst, OUTPUT);
  if (pin_int >= 0) pinMode(pin_int, OUTPUT);
  digitalWrite(pin_rst, LOW);
  if (pin_int >= 0) digitalWrite(pin_int, LOW);
  delay(11);
  digitalWrite(pin_rst, HIGH);
  delay(6);
  if (pin_int >= 0) pinMode(pin_int, INPUT);
  delay(55);
}

bool gt911_init(int sda, int scl, int pin_int, int pin_rst) {
  reset_select_0x5d(pin_int, pin_rst);
  Wire.begin(sda, scl, 400000);
  const uint8_t addrs[2] = {0x5D, 0x14};
  for (uint8_t a : addrs) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() != 0) continue;
    s_addr = a;
    uint8_t id[4] = {0};
    rd_regs(0x8140, id, 4);
    uint8_t res[4] = {0};
    if (rd_regs(0x8048, res, 4)) {
      s_w = res[0] | (res[1] << 8);
      s_h = res[2] | (res[3] << 8);
    }
    pd_log("[touch] GT911 @0x%02X id=%c%c%c%c res=%ux%u\n", a,
                  id[0], id[1], id[2], id[3] ? id[3] : ' ', s_w, s_h);
    return true;
  }
  pd_log("[touch] GT911 not found on I2C bus\n");
  return false;
}

bool gt911_read(int16_t *x, int16_t *y, bool *pressed) {
  uint8_t status;
  if (!rd_regs(0x814E, &status, 1)) return false;
  if (!(status & 0x80)) {
    /* No new frame yet: report the last known state so a held finger doesn't
     * flicker between pressed/released at the LVGL poll rate. */
    *pressed = s_last_pressed;
    *x = s_last_x;
    *y = s_last_y;
    return true;
  }
  uint8_t n = status & 0x0F;
  if (n > 0 && n <= 5) {
    uint8_t d[6];
    if (rd_regs(0x8150, d, 6)) {
      s_last_x = (int16_t)(d[0] | (d[1] << 8));
      s_last_y = (int16_t)(d[2] | (d[3] << 8));
      s_last_pressed = true;
    }
  } else {
    s_last_pressed = false;
  }
  wr_reg8(0x814E, 0);
  *pressed = s_last_pressed;
  *x = s_last_x;
  *y = s_last_y;
  return true;
}

void gt911_resolution(uint16_t *w, uint16_t *h) {
  *w = s_w;
  *h = s_h;
}
