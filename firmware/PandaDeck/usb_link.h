/* The desktop app's USB link: framed requests on the CDC serial port
 * (docs/protocol.md; framing in link_codec.cpp, handlers in api.cpp).
 *
 * Receive: the CDC RX event (Arduino's USB event task) decodes bytes as they
 * arrive, so the port is drained even while the link task is busy with an
 * HTTP request: JSON / `D` payloads go into one 64 KB PSRAM buffer, `I`
 * pixels straight into the icon / backdrop staging buffer. Handle: the link
 * task (net.cpp, shared with HTTP, never the UI thread) answers each
 * finished frame with exactly one reply, then receiving resumes. */
#pragma once
#include <stdint.h>

void usb_link_init(); /* after Serial.begin() + pdlog_init() */
/* Link task: answers a finished frame, drops stalled ones. True if it did work. */
bool usb_link_poll();
/* A host completed a valid request since boot (OTA health check). */
bool usb_link_seen();

struct UsbLinkStats {
  uint32_t frames;    /* requests handled */
  uint32_t dropped;   /* bad headers + stalled frames */
  uint32_t overflows; /* CDC queue overflows (bytes lost before us) */
  bool host_open;     /* a host has the port open (DTR) */
};
UsbLinkStats usb_link_stats();
