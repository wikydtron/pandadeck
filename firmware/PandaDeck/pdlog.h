/* Log lines on the USB CDC port. The same port carries the binary link
 * frames (usb_link.cpp), so every write goes through one lock: a log line
 * can never land in the middle of a frame. Logging is best-effort: if the
 * port is busy with a frame for long, the line is dropped rather than
 * stalling the caller (the UI logs from its own thread). */
#pragma once
#include <stddef.h>
#include <stdint.h>

void pdlog_init(); /* right after Serial.begin() */
void pd_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* Exclusive Serial TX for one binary frame. */
bool pd_serial_lock(uint32_t timeout_ms);
void pd_serial_unlock();
