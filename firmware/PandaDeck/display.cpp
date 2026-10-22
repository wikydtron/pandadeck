#include "display.h"
#include "board_panda.h"
#include "gt911.h"

#include <Arduino.h>
#include <Preferences.h>

#include "pdlog.h"
#include <driver/gpio.h>
#include <driver/ledc.h>
#include <esp_heap_caps.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_rgb.h>
#include <freertos/semphr.h>

/* ==========================================================================
 * Rendering model
 *
 * The Panda Touch's panel is mounted rotated 180 deg relative to its scan
 * direction, so LVGL renders in its natural (user) orientation into small
 * PSRAM chunks (PARTIAL mode) and flush_cb copies each chunk rotated into the
 * panel's *back* framebuffer. On the last chunk of a frame we flip the panel
 * to the back buffer, wait until the driver has scanned the new buffer for a
 * full frame (bounce-buffer mode prefetches, so one "frame done" is not
 * enough), then copy just the changed rectangles into the old buffer so both
 * stay identical. LVGL never touches a buffer that is on screen -> no tearing.
 * ========================================================================== */

#define DIRTY_MAX 48
#define DRAW_BUF_LINES 96     /* PSRAM fallback */
#define DRAW_BUF_LINES_SRAM 16 /* preferred: render in internal SRAM */
#define REFR_PERIOD_MS 33      /* ~30 fps; the custom-sdkconfig core has the PSRAM headroom */
#define SRAM_HEADROOM (64 * 1024)

struct Rect {
  int16_t x1, y1, x2, y2; /* panel coordinates, inclusive */
};

static esp_lcd_panel_handle_t s_panel = nullptr;
static lv_display_t *s_disp = nullptr;
static SemaphoreHandle_t s_sem_frame_done = nullptr;
static uint16_t *s_front = nullptr; /* being scanned out */
static uint16_t *s_back = nullptr;  /* flush_cb writes here */
static Rect s_dirty[DIRTY_MAX];
static int s_n_dirty = 0;
static bool s_dirty_overflow = false;
static volatile uint32_t s_fb_done_count = 0;

static uint8_t s_bl_target = 100;
static uint8_t s_bl_now = 0;
static bool s_bl_override = false; /* debug probing owns the pin */
static uint8_t s_bl_mode = 0;      /* 0 none, 1 LEDC PWM, 2 plain GPIO on/off */
static uint32_t s_last_touch_ms = 0;
static uint32_t s_frames = 0;
static uint32_t s_pclk_hz = PANEL_PCLK_HZ;
static uint32_t s_touches = 0;
static bool s_touch_ok = false;
static volatile int16_t s_raw_x = -1, s_raw_y = -1;

/* Orientation flips, persisted in NVS "disp": `flip` = picture, `tflip` =
 * touch. They are separate because this panel can come up scanning the other
 * way after a power cycle (picture flips, touch doesn't). Picture changes are
 * applied on the LVGL thread (display_apply_pending_flip). */
static volatile bool s_flip = false;
static volatile bool s_tflip = false;
static volatile int s_flip_pending = -1; /* -1 none, else 0/1 requested */
static inline bool disp_rot() { return (DISPLAY_ROTATE_180 != 0) != s_flip; }
static inline bool touch_rot() { return (TOUCH_ROTATE_180 != 0) != s_tflip; }

static bool IRAM_ATTR on_frame_done(esp_lcd_panel_handle_t, const esp_lcd_rgb_panel_event_data_t *, void *) {
  s_fb_done_count++;
  BaseType_t woken = pdFALSE;
  xSemaphoreGiveFromISR(s_sem_frame_done, &woken);
  return woken == pdTRUE;
}

/* ---- /api/screen snapshot ---------------------------------------------- */
static volatile bool s_snap_req = false;
static uint16_t *s_snap = nullptr;
static SemaphoreHandle_t s_snap_done = nullptr;

/* ---- flush ---------------------------------------------------------------- */
static void to_panel_rect(const lv_area_t *a, Rect *r) {
  if (disp_rot()) {
    r->x1 = PANEL_W - 1 - a->x2;
    r->x2 = PANEL_W - 1 - a->x1;
    r->y1 = PANEL_H - 1 - a->y2;
    r->y2 = PANEL_H - 1 - a->y1;
  } else {
    r->x1 = a->x1;
    r->x2 = a->x2;
    r->y1 = a->y1;
    r->y2 = a->y2;
  }
}

static void copy_chunk(const lv_area_t *a, const uint16_t *src) {
  const int w = a->x2 - a->x1 + 1;
  for (int y = a->y1; y <= a->y2; y++) {
    const uint16_t *s = src + (size_t)(y - a->y1) * w;
    if (disp_rot()) {
      uint16_t *d = s_back + (size_t)(PANEL_H - 1 - y) * PANEL_W + (PANEL_W - 1 - a->x1);
      for (int i = 0; i < w; i++) *d-- = s[i];
    } else {
      memcpy(s_back + (size_t)y * PANEL_W + a->x1, s, (size_t)w * 2);
    }
  }
}

static void wait_frames(int n) {
  for (int i = 0; i < n; i++) xSemaphoreTake(s_sem_frame_done, pdMS_TO_TICKS(50));
}

static void sync_back_from_front() {
  if (s_dirty_overflow) {
    memcpy(s_back, s_front, PANEL_W * PANEL_H * 2);
  } else {
    for (int i = 0; i < s_n_dirty; i++) {
      const Rect &r = s_dirty[i];
      const size_t bytes = (size_t)(r.x2 - r.x1 + 1) * 2;
      for (int y = r.y1; y <= r.y2; y++) {
        const size_t off = (size_t)y * PANEL_W + r.x1;
        memcpy(s_back + off, s_front + off, bytes);
      }
    }
  }
  s_n_dirty = 0;
  s_dirty_overflow = false;
}

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px) {
  copy_chunk(area, (const uint16_t *)px);
  if (s_n_dirty < DIRTY_MAX) to_panel_rect(area, &s_dirty[s_n_dirty++]);
  else s_dirty_overflow = true;

  if (lv_display_flush_is_last(disp)) {
    xSemaphoreTake(s_sem_frame_done, 0); /* drop a stale event */
    esp_lcd_panel_draw_bitmap(s_panel, 0, 0, PANEL_W, PANEL_H, s_back); /* flip */
    /* Two "frame buffer complete" events: the safe point to reuse the old
     * buffer, and a natural throttle - every frame LVGL renders is PSRAM
     * traffic competing with the display DMA (fewer frames = no underruns).
     * Touch is sampled by its own task, so this doesn't cost responsiveness. */
    wait_frames(2);
    uint16_t *t = s_front;
    s_front = s_back;
    s_back = t;
    sync_back_from_front();
    s_frames++;
    if (s_snap_req && s_snap) {
      memcpy(s_snap, s_front, PANEL_W * PANEL_H * 2);
      s_snap_req = false;
      xSemaphoreGive(s_snap_done);
    }
  }
  lv_display_flush_ready(disp);
}

const uint16_t *display_snapshot(uint32_t timeout_ms) {
  if (!s_front) return nullptr;
  if (!s_snap) s_snap = (uint16_t *)ps_malloc(PANEL_W * PANEL_H * 2);
  if (!s_snap) return nullptr;
  xSemaphoreTake(s_snap_done, 0);
  s_snap_req = true;
  if (xSemaphoreTake(s_snap_done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
    /* Idle screen: the front buffer is stable, copy it directly. */
    s_snap_req = false;
    memcpy(s_snap, s_front, PANEL_W * PANEL_H * 2);
  }
  return s_snap;
}

const uint16_t *display_front_buffer() { return s_front; }

/* ---- touch -------------------------------------------------------------------
 * A dedicated task samples the GT911 every 10 ms and latches presses, so a
 * quick tap is never lost while the render loop is busy (a full-screen redraw
 * can take a few hundred ms on this panel). LVGL's read_cb just consumes the
 * latched state: every press yields at least one PRESSED report, then
 * RELEASED once the finger is up => taps always become clicks. */
#define TOUCH_POLL_MS 10

static portMUX_TYPE s_touch_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_t_down = false;       /* finger currently down */
static int16_t s_t_x = 0, s_t_y = 0; /* current (or last) UI coordinates */
static int16_t s_t_down_x = 0, s_t_down_y = 0;
static uint32_t s_t_press_seq = 0;  /* increments on every new press */
static bool s_t_swallow = false;    /* this press only wakes the screen */
static volatile int s_inj_x = 0, s_inj_y = 0;
static volatile uint32_t s_inj_until = 0;

void display_inject_touch(int x, int y, int ms) {
  s_inj_x = x;
  s_inj_y = y;
  s_inj_until = millis() + (uint32_t)(ms < 20 ? 20 : ms);
}

static void touch_task(void *) {
  for (;;) {
    int16_t x = 0, y = 0;
    bool pressed = false;
    if ((int32_t)(s_inj_until - millis()) > 0) {
      x = (int16_t)s_inj_x; /* injected taps are already in UI coordinates */
      y = (int16_t)s_inj_y;
      pressed = true;
    } else if (s_touch_ok && gt911_read(&x, &y, &pressed) && pressed) {
      s_raw_x = x;
      s_raw_y = y;
      if (touch_rot()) {
        x = PANEL_W - 1 - x;
        y = PANEL_H - 1 - y;
      }
    }
    if (x < 0) x = 0;
    if (y < 0) y = 0;
    if (x >= PANEL_W) x = PANEL_W - 1;
    if (y >= PANEL_H) y = PANEL_H - 1;

    portENTER_CRITICAL(&s_touch_mux);
    if (pressed && !s_t_down) {
      s_t_press_seq++;
      s_t_down_x = x;
      s_t_down_y = y;
      /* A touch on a dimmed screen only wakes it: never let that first tap
       * land on a button (e.g. Deny/Restart) you couldn't see. */
      s_t_swallow = s_bl_now < 50;
      if (s_t_swallow) s_bl_target = 100;
      s_touches++;
    }
    s_t_down = pressed;
    if (pressed) {
      s_t_x = x;
      s_t_y = y;
    }
    portEXIT_CRITICAL(&s_touch_mux);
    if (pressed) s_last_touch_ms = millis();
    vTaskDelay(pdMS_TO_TICKS(TOUCH_POLL_MS));
  }
}

static void touch_read_cb(lv_indev_t *, lv_indev_data_t *data) {
  static uint32_t s_seen_seq = 0;
  portENTER_CRITICAL(&s_touch_mux);
  bool down = s_t_down, swallow = s_t_swallow;
  uint32_t seq = s_t_press_seq;
  int16_t x = s_t_x, y = s_t_y, dx = s_t_down_x, dy = s_t_down_y;
  portEXIT_CRITICAL(&s_touch_mux);

  if (seq != s_seen_seq) {
    /* A press started since the last read (maybe already released). Report it
     * at its press-down point once, so LVGL registers the tap. */
    s_seen_seq = seq;
    if (!swallow) {
      data->point.x = dx;
      data->point.y = dy;
      data->state = LV_INDEV_STATE_PRESSED;
      return;
    }
  }
  if (down && !swallow) {
    data->point.x = x;
    data->point.y = y;
    data->state = LV_INDEV_STATE_PRESSED;
    return;
  }
  data->state = LV_INDEV_STATE_RELEASED;
}

static uint32_t tick_cb() { return millis(); }

/* ---- backlight ------------------------------------------------------------------
 * Driven through the IDF LEDC driver (the Arduino ledcAttach path never
 * reached the pin on this unit). A self-test reads the pad back at 0% and
 * 100%; if PWM doesn't show up on the pin we fall back to plain on/off. */
#define BL_LEDC_TIMER LEDC_TIMER_1
#define BL_LEDC_CH LEDC_CHANNEL_7
#define BL_FULL (1u << BL_PWM_BITS)

static void bl_ledc_set(uint32_t duty) {
  ledc_set_duty(LEDC_LOW_SPEED_MODE, BL_LEDC_CH, duty);
  ledc_update_duty(LEDC_LOW_SPEED_MODE, BL_LEDC_CH);
}

static bool bl_ledc_init_and_test() {
  ledc_timer_config_t t = {};
  t.speed_mode = LEDC_LOW_SPEED_MODE;
  t.timer_num = BL_LEDC_TIMER;
  t.duty_resolution = (ledc_timer_bit_t)BL_PWM_BITS;
  t.freq_hz = BL_PWM_FREQ;
  t.clk_cfg = LEDC_AUTO_CLK;
  if (ledc_timer_config(&t) != ESP_OK) return false;
  ledc_channel_config_t c = {};
  c.speed_mode = LEDC_LOW_SPEED_MODE;
  c.channel = BL_LEDC_CH;
  c.timer_sel = BL_LEDC_TIMER;
  c.intr_type = LEDC_INTR_DISABLE;
  c.gpio_num = PANEL_PIN_BL;
  c.duty = 0;
  c.hpoint = 0;
  if (ledc_channel_config(&c) != ESP_OK) return false;
  gpio_input_enable((gpio_num_t)PANEL_PIN_BL); /* read the pad back */
  bl_ledc_set(0);
  delayMicroseconds(300);
  int lo = gpio_get_level((gpio_num_t)PANEL_PIN_BL);
  bl_ledc_set(BL_FULL);
  delayMicroseconds(300);
  int hi = gpio_get_level((gpio_num_t)PANEL_PIN_BL);
  bl_ledc_set(0);
  pd_log("[bl] LEDC self-test lo=%d hi=%d\n", lo, hi);
  return lo == 0 && hi == 1;
}

static void bl_hw_init() {
  if (bl_ledc_init_and_test()) {
    s_bl_mode = 1;
    return;
  }
  ledc_stop(LEDC_LOW_SPEED_MODE, BL_LEDC_CH, 0);
  gpio_reset_pin((gpio_num_t)PANEL_PIN_BL);
  pinMode(PANEL_PIN_BL, OUTPUT);
  digitalWrite(PANEL_PIN_BL, LOW);
  s_bl_mode = 2;
}

static void bl_write(uint8_t pct) {
  if (s_bl_override) return;
  if (s_bl_mode == 1) {
    /* Perceptual-ish curve: low percentages stay usable at night. */
    uint32_t duty = (uint32_t)pct * pct * BL_FULL / 10000u;
    if (pct > 0 && duty < 8) duty = 8;
    bl_ledc_set(duty);
  } else if (s_bl_mode == 2) {
    digitalWrite(PANEL_PIN_BL, pct >= 15 ? HIGH : LOW);
  }
}

static void bl_ramp_timer(lv_timer_t *) {
  if (s_bl_now == s_bl_target) return;
  int step = (int)s_bl_target - (int)s_bl_now;
  if (step > 4) step = 4;
  if (step < -4) step = -4;
  s_bl_now = (uint8_t)((int)s_bl_now + step);
  bl_write(s_bl_now);
}

void display_set_brightness(uint8_t pct) { s_bl_target = pct > 100 ? 100 : pct; }
uint8_t display_brightness() { return s_bl_target; }
uint32_t display_idle_ms() { return millis() - s_last_touch_ms; }
void display_poke() { s_last_touch_ms = millis(); }

/* ---- debug probing (HTTP task) ------------------------------------------------- */
static bool pin_is_probe_safe(int pin) {
  /* Only pins the BSP doesn't assign (plus BL/RST). Never flash/PSRAM
   * (26-37), LCD data, touch, USB or UART. */
  return pin == 0 || pin == PANEL_PIN_BL || pin == 42 || pin == 45 || pin == PANEL_PIN_RST || pin == 3 || pin == 4;
}

bool display_debug_gpio(int pin, int level) {
  if (!pin_is_probe_safe(pin)) return false;
  if (pin == PANEL_PIN_BL) {
    s_bl_override = true;
    if (s_bl_mode == 1) ledc_stop(LEDC_LOW_SPEED_MODE, BL_LEDC_CH, level ? 1 : 0);
    gpio_reset_pin((gpio_num_t)pin);
  }
  pinMode(pin, OUTPUT);
  digitalWrite(pin, level ? HIGH : LOW);
  return true;
}

void display_debug_bl(int duty, bool, bool release) {
  if (release) {
    s_bl_override = false;
    bl_hw_init();
    bl_write(s_bl_now);
    return;
  }
  s_bl_override = true;
  if (s_bl_mode == 1) bl_ledc_set((uint32_t)duty);
  else digitalWrite(PANEL_PIN_BL, duty > 0 ? HIGH : LOW);
}

bool display_debug_pclk(uint32_t hz) {
  if (!s_panel || hz < 8000000 || hz > 30000000) return false;
  if (esp_lcd_rgb_panel_set_pclk(s_panel, hz) != ESP_OK) return false;
  s_pclk_hz = hz;
  return true;
}

uint32_t display_pclk() { return s_pclk_hz; }

DisplayStats display_stats() {
  DisplayStats st{};
  st.fb_done = s_fb_done_count;
  st.bl_now = s_bl_now;
  st.bl_override = s_bl_override;
  st.bl_mode = s_bl_mode;
  st.frames = s_frames;
  st.touches = s_touches;
  st.touch_ok = s_touch_ok;
  st.raw_x = s_raw_x;
  st.raw_y = s_raw_y;
  gt911_resolution(&st.touch_w, &st.touch_h);
  return st;
}

/* ---- persisted panel config ----------------------------------------------------------
 * Tunables that decide whether this panel streams cleanly (see board_panda.h):
 * bounce-buffer lines (0 = DMA reads PSRAM directly), pixel clock, PCLK edge.
 * Stored in NVS so they can be tried remotely without reflashing. */
static DisplayCfg s_cfg = {PANEL_BOUNCE_LINES, PANEL_PCLK_HZ, true};

static void cfg_load() {
  Preferences p;
  if (!p.begin("disp", true)) return;
  s_cfg.bounce_lines = p.getUChar("bb", s_cfg.bounce_lines);
  s_cfg.pclk_hz = p.getUInt("pclk", s_cfg.pclk_hz);
  s_cfg.pclk_neg = p.getBool("neg", s_cfg.pclk_neg);
  s_flip = p.getBool("flip", false);
  s_tflip = p.getBool("tflip", false);
  p.end();
  if (s_cfg.bounce_lines > 40) s_cfg.bounce_lines = 40;
  if (s_cfg.pclk_hz < 10000000 || s_cfg.pclk_hz > 30000000) s_cfg.pclk_hz = PANEL_PCLK_HZ;
}

bool display_save_cfg(const DisplayCfg &c) {
  if (c.bounce_lines > 40 || c.pclk_hz < 10000000 || c.pclk_hz > 30000000) return false;
  Preferences p;
  if (!p.begin("disp", false)) return false;
  p.putUChar("bb", c.bounce_lines);
  p.putUInt("pclk", c.pclk_hz);
  p.putBool("neg", c.pclk_neg);
  p.end();
  return true;
}

DisplayCfg display_cfg() { return s_cfg; }

bool display_flipped() { return s_flip; }
bool display_touch_flipped() { return s_tflip; }
void display_request_flip(bool flip) { s_flip_pending = flip ? 1 : 0; }

bool display_flip_wanted() {
  int p = s_flip_pending;
  return p >= 0 ? p == 1 : s_flip;
}

void display_forget_orientation() {
  Preferences p;
  if (p.begin("disp", false)) {
    p.remove("flip");
    p.remove("tflip");
    p.end();
  }
}

void display_set_touch_flip(bool flip) {
  s_tflip = flip;
  Preferences p;
  if (p.begin("disp", false)) {
    p.putBool("tflip", flip);
    p.end();
  }
}

/* Debug: pulse the panel's reset line (GPIO46) so it re-latches whatever it
 * samples at reset (scan direction?). The RGB stream keeps running. */
void display_debug_lcd_reset() {
  pinMode(PANEL_PIN_RST, OUTPUT);
  digitalWrite(PANEL_PIN_RST, LOW);
  delay(20);
  digitalWrite(PANEL_PIN_RST, HIGH);
}

/* LVGL thread: apply a requested flip between frames, persist it, redraw all
 * (the full invalidation overflows the dirty list -> whole-buffer copy). */
void display_apply_pending_flip() {
  int want = s_flip_pending;
  if (want < 0) return;
  s_flip_pending = -1;
  if ((want == 1) == s_flip) return;
  s_flip = want == 1;
  Preferences p;
  if (p.begin("disp", false)) {
    p.putBool("flip", s_flip);
    p.end();
  }
  lv_obj_invalidate(lv_screen_active());
  lv_obj_invalidate(lv_layer_top());
  pd_log("[disp] orientation flip -> %d\n", (int)s_flip);
}

/* ---- init ------------------------------------------------------------------------- */
static bool panel_init() {
  cfg_load();
  s_pclk_hz = s_cfg.pclk_hz;
  pinMode(PANEL_PIN_RST, OUTPUT);
  digitalWrite(PANEL_PIN_RST, LOW);
  delay(50);
  digitalWrite(PANEL_PIN_RST, HIGH);
  delay(50);

  esp_lcd_rgb_panel_config_t cfg = {};
  cfg.clk_src = LCD_CLK_SRC_DEFAULT;
  cfg.timings.pclk_hz = s_cfg.pclk_hz;
  cfg.timings.h_res = PANEL_W;
  cfg.timings.v_res = PANEL_H;
  cfg.timings.hsync_pulse_width = PANEL_HSYNC_PW;
  cfg.timings.hsync_back_porch = PANEL_HSYNC_BP;
  cfg.timings.hsync_front_porch = PANEL_HSYNC_FP;
  cfg.timings.vsync_pulse_width = PANEL_VSYNC_PW;
  cfg.timings.vsync_back_porch = PANEL_VSYNC_BP;
  cfg.timings.vsync_front_porch = PANEL_VSYNC_FP;
  cfg.timings.flags.pclk_active_neg = s_cfg.pclk_neg;
  cfg.data_width = 16;
  cfg.bits_per_pixel = 16;
  cfg.num_fbs = 2;
  cfg.bounce_buffer_size_px = PANEL_W * s_cfg.bounce_lines;
  cfg.hsync_gpio_num = -1;
  cfg.vsync_gpio_num = -1;
  cfg.de_gpio_num = PANEL_PIN_DE;
  cfg.pclk_gpio_num = PANEL_PIN_PCLK;
  cfg.disp_gpio_num = -1;
  const int pins[16] = PANEL_DATA_PINS;
  for (int i = 0; i < 16; i++) cfg.data_gpio_nums[i] = pins[i];
  cfg.flags.fb_in_psram = 1;

  esp_err_t err = esp_lcd_new_rgb_panel(&cfg, &s_panel);
  if (err != ESP_OK) {
    pd_log("[disp] esp_lcd_new_rgb_panel failed: %s\n", esp_err_to_name(err));
    return false;
  }
  esp_lcd_rgb_panel_event_callbacks_t cbs = {};
  cbs.on_frame_buf_complete = on_frame_done;
  esp_lcd_rgb_panel_register_event_callbacks(s_panel, &cbs, nullptr);
  esp_lcd_panel_reset(s_panel);
  esp_lcd_panel_init(s_panel);
  void *fb0 = nullptr, *fb1 = nullptr;
  esp_lcd_rgb_panel_get_frame_buffer(s_panel, 2, &fb0, &fb1);
  memset(fb0, 0, PANEL_W * PANEL_H * 2);
  memset(fb1, 0, PANEL_W * PANEL_H * 2);
  s_front = (uint16_t *)fb0; /* driver starts scanning fb0 */
  s_back = (uint16_t *)fb1;
  pd_log("[disp] RGB panel up: fb0=%p fb1=%p\n", fb0, fb1);
  return true;
}

bool display_init() {
  bl_hw_init();
  s_sem_frame_done = xSemaphoreCreateBinary();
  s_snap_done = xSemaphoreCreateBinary();
  if (!panel_init()) return false;

  lv_init();
  lv_tick_set_cb(tick_cb);
  s_disp = lv_display_create(PANEL_W, PANEL_H);
  lv_display_set_color_format(s_disp, LV_COLOR_FORMAT_RGB565);
  /* LVGL blends text/shapes/icons in a small *internal* SRAM buffer, so only
   * finished pixels cross into PSRAM (one sequential rotated copy). Rendering
   * straight into PSRAM through the 32 KB data cache was the main source of
   * bus contention that starved the display DMA (jumping/shifted lines). */
  size_t buf_bytes = PANEL_W * DRAW_BUF_LINES_SRAM * 2;
  void *b1 = nullptr;
  void *b2 = nullptr;
  /* Only if it leaves real headroom: WiFi needs bursts of internal RAM, and a
   * 64 KB data-cache build already spends 32 KB more of it. */
  if (heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) > buf_bytes + SRAM_HEADROOM)
    b1 = heap_caps_aligned_alloc(64, buf_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (!b1) {
    buf_bytes = PANEL_W * DRAW_BUF_LINES * 2;
    b1 = heap_caps_aligned_alloc(64, buf_bytes, MALLOC_CAP_SPIRAM);
    b2 = heap_caps_aligned_alloc(64, buf_bytes, MALLOC_CAP_SPIRAM);
    pd_log("[disp] internal draw buffer unavailable, using PSRAM\n");
  }
  if (!b1) {
    pd_log("[disp] draw buffer alloc failed\n");
    return false;
  }
  lv_display_set_buffers(s_disp, b1, b2, buf_bytes, LV_DISPLAY_RENDER_MODE_PARTIAL);
  lv_timer_set_period(lv_display_get_refr_timer(s_disp), REFR_PERIOD_MS);
  lv_display_set_flush_cb(s_disp, flush_cb);

  s_touch_ok = gt911_init(TOUCH_PIN_SDA, TOUCH_PIN_SCL, TOUCH_PIN_INT, TOUCH_PIN_RST);
  /* Only the touch task touches Wire from here on. */
  xTaskCreatePinnedToCore(touch_task, "touch", 3072, nullptr, 5, nullptr, 0);
  lv_indev_t *indev = lv_indev_create();
  lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
  lv_indev_set_read_cb(indev, touch_read_cb);
  lv_indev_set_display(indev, s_disp);

  s_last_touch_ms = millis();
  lv_timer_create(bl_ramp_timer, 15, nullptr);
  return true;
}
