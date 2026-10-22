#include "pdlog.h"

#include <Arduino.h>
#include <freertos/semphr.h>
#include <stdarg.h>

#define LOG_WAIT_MS 50

static SemaphoreHandle_t s_tx = nullptr;

void pdlog_init() {
  if (!s_tx) s_tx = xSemaphoreCreateMutex();
}

bool pd_serial_lock(uint32_t timeout_ms) {
  if (!s_tx) return true;
  return xSemaphoreTake(s_tx, pdMS_TO_TICKS(timeout_ms)) == pdTRUE;
}

void pd_serial_unlock() {
  if (s_tx) xSemaphoreGive(s_tx);
}

void pd_log(const char *fmt, ...) {
  char line[200];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(line, sizeof line, fmt, ap);
  va_end(ap);
  if (n <= 0) return;
  if ((size_t)n >= sizeof line) n = sizeof line - 1;
  if (!pd_serial_lock(LOG_WAIT_MS)) return; /* a big frame is going out: drop */
  Serial.write((const uint8_t *)line, (size_t)n);
  pd_serial_unlock();
}
