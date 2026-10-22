/* PandaDeck — BIGTREETECH Panda Touch reborn as a desk command center:
 *   AI      live Claude Code / Codex sessions + approve/deny permission prompts
 *   DECK    touch Stream Deck (USB HID keyboard/media keys + app actions)
 *   MUSIC   now playing on the computer
 *   HUD     your PC's CPU/GPU + an optional server's health, container restarts
 *
 * Board: ESP32-S3R8 (16MB flash, 8MB OPI PSRAM), 800x480 RGB + GT911.
 * Data: the desktop app pushes snapshots over USB (usb_link.cpp) or Wi-Fi
 * (POST /api/sync), see docs/protocol.md. Pages, Wi-Fi and brightness live
 * in NVS (settings.cpp); config.h is optional (features.h).
 * Build: see README.md (PlatformIO, TinyUSB mode). */
#include <Arduino.h>
#include <lvgl.h>

#include "display.h"
#include "hid.h"
#include "net.h"
#include "pdlog.h"
#include "settings.h"
#include "store.h"
#include "ui.h"
#include "usb_link.h"

static bool s_ui_ok = false;
volatile bool g_ui_frozen = false;
/* Render-loop timing for /api/info: worst lv_timer_handler() in the last
 * ~5 s and a running average, so UI lag is measured, not guessed. */
volatile uint32_t g_loop_max_ms = 0, g_loop_avg_ms = 0;
/* LVGL renders on the loop task. 8 KB (Arduino's default) overflowed on the
 * busiest page (canary panic, corrupt backtrace); keep a wide margin and report
 * the high-water mark in /api/info ("loop_stack_free"). */
SET_LOOP_TASK_STACK_SIZE(12 * 1024);  /* measured peak ~8.2 KB (all pages) */
volatile uint32_t g_loop_stack_free = 0;

/* Crash-loop guard: survives software resets (not power loss). After 3
 * consecutive crash reboots we skip the display entirely and run headless
 * (USB + Wi-Fi + OTA only) so a bad build can always be replaced. */
#define SAFE_MAGIC 0x50414E44u /* "PAND" */
#define SAFE_AFTER 3
RTC_NOINIT_ATTR static uint32_t s_rtc_magic;
RTC_NOINIT_ATTR static uint32_t s_rtc_crashes;

static bool crash_reset(esp_reset_reason_t r) {
  return r == ESP_RST_PANIC || r == ESP_RST_INT_WDT || r == ESP_RST_TASK_WDT || r == ESP_RST_WDT;
}

static bool enter_safe_mode() {
  if (s_rtc_magic != SAFE_MAGIC) {
    s_rtc_magic = SAFE_MAGIC;
    s_rtc_crashes = 0;
  }
  s_rtc_crashes = crash_reset(esp_reset_reason()) ? s_rtc_crashes + 1 : 0;
  return s_rtc_crashes >= SAFE_AFTER;
}

void setup() {
  /* USB first: the HID interfaces must exist before the host enumerates. */
  hid_init();
  Serial.begin(115200);
  pdlog_init();
  delay(200);
  bool safe = enter_safe_mode();
  pd_log("\n[boot] PandaDeck %s  psram=%u reset=%d crashes=%lu%s\n", FW_VERSION, (unsigned)ESP.getPsramSize(),
         (int)esp_reset_reason(), (unsigned long)s_rtc_crashes, safe ? "  SAFE MODE" : "");

  settings_init();
  store_init();
  usb_link_init();
  /* Links before the display: if the panel bring-up ever wedges, OTA (USB
   * or Wi-Fi) is still reachable to push a fixed image. Wi-Fi never blocks:
   * the USB link works without it. */
  net_init();

  if (safe) {
    pd_log("[boot] safe mode: display skipped, OTA only\n");
    return;
  }
  if (!display_init()) {
    pd_log("[boot] display init FAILED - running headless (OTA still up)\n");
    return;
  }
  ui_init();
  s_ui_ok = true;
  pd_log("[boot] ui up\n");
}

void loop() {
  uint32_t wait = 10;
  if (s_ui_ok && !g_ui_frozen) {
    static uint32_t s_win_start = 0, s_win_max = 0, s_avg_x16 = 0;
    uint32_t t0 = millis();
    wait = lv_timer_handler();
    uint32_t dt = millis() - t0;
    if (dt > s_win_max) s_win_max = dt;
    s_avg_x16 = s_avg_x16 - (s_avg_x16 >> 4) + dt; /* EMA, alpha 1/16 */
    g_loop_avg_ms = s_avg_x16 >> 4;
    g_loop_stack_free = uxTaskGetStackHighWaterMark(nullptr);
    if (millis() - s_win_start > 5000) {
      g_loop_max_ms = s_win_max;
      s_win_max = 0;
      s_win_start = millis();
    }
  }
  net_mark_valid_if_healthy();
  /* Up for a minute: whatever crashed before is not a boot loop. */
  if (s_rtc_crashes && millis() > 60000) s_rtc_crashes = 0;
  if (wait > 10) wait = 10;
  delay(wait ? wait : 1);
}
