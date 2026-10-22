/* USB link framing (docs/protocol.md), pure C++ so it is host-tested.
 *
 *   frame := MAGIC(3) TYPE(1) LEN(4, little-endian u32) PAYLOAD(LEN)
 *   MAGIC := 0x1B 'P' 'D'
 *
 * Bytes outside a frame (log text, line noise) are skipped while scanning
 * for the next MAGIC. The decoder is incremental: feed it whatever arrived,
 * in chunks of any size; payload bytes are handed to a LinkSink as they come
 * so a large image never needs a second full-size buffer. */
#pragma once
#include <stddef.h>
#include <stdint.h>

#define LINK_MAGIC0 0x1B
#define LINK_MAGIC1 'P'
#define LINK_MAGIC2 'D'
#define LINK_HEADER_LEN 8
#define LINK_MAX_PAYLOAD (512u * 1024u)

/* The 8 header bytes of a frame. */
void link_encode_header(uint8_t out[LINK_HEADER_LEN], char type, uint32_t len);
/* Reply TYPE for a request TYPE: its lowercase ('S' -> 's'). */
char link_reply_type(char request_type);

class LinkSink {
 public:
  virtual ~LinkSink() {}
  /* A valid header arrived. Return false to have the payload skipped
   * (frame_data is then not called, frame_end still is). */
  virtual bool frame_begin(char type, uint32_t len) = 0;
  virtual void frame_data(const uint8_t *data, size_t n) = 0;
  /* All `len` payload bytes consumed. accepted = what frame_begin returned. */
  virtual void frame_end(char type, bool accepted) = 0;
  /* A frame in progress was dropped (stalled / reset); no frame_end follows. */
  virtual void frame_abort(char type) { (void)type; }
};

class LinkDecoder {
 public:
  explicit LinkDecoder(uint32_t max_payload = LINK_MAX_PAYLOAD);

  /* Consumes bytes until one frame completes (frame_end was called) or the
   * input runs out, and returns how many were consumed: the caller handles
   * the finished frame, then feeds the rest. now_ms stamps activity for
   * check_timeout(). */
  size_t feed(const uint8_t *data, size_t n, LinkSink &sink, uint32_t now_ms);

  /* Drops a frame that got no byte for timeout_ms (lost bytes would
   * otherwise make the next request's bytes count as this payload).
   * Returns true if a frame was dropped. */
  bool check_timeout(uint32_t now_ms, uint32_t timeout_ms, LinkSink &sink);

  /* Back to scanning; aborts a frame in progress (sink may be null). */
  void reset(LinkSink *sink);

  bool in_frame() const { return state_ != SCAN; }
  /* Most bytes the next feed() can take without reading past the end of a
   * frame (the reader pulls exactly this much, so nothing is left over). */
  uint32_t need() const;
  uint32_t frames() const { return frames_; }   /* completed frames */
  uint32_t dropped() const { return dropped_; } /* bad headers + timeouts */

 private:
  enum State { SCAN, HEADER, PAYLOAD };
  State state_;
  uint8_t magic_;     /* MAGIC bytes matched so far while scanning */
  uint8_t hdr_[5];    /* TYPE + LEN */
  uint8_t hdr_n_;
  char type_;
  bool accepted_;
  uint32_t len_, left_, max_;
  uint32_t last_ms_;
  uint32_t frames_, dropped_;
};
