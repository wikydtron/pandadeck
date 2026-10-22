#include "link_codec.h"

void link_encode_header(uint8_t out[LINK_HEADER_LEN], char type, uint32_t len) {
  out[0] = LINK_MAGIC0;
  out[1] = LINK_MAGIC1;
  out[2] = LINK_MAGIC2;
  out[3] = (uint8_t)type;
  out[4] = (uint8_t)(len & 0xFF);
  out[5] = (uint8_t)((len >> 8) & 0xFF);
  out[6] = (uint8_t)((len >> 16) & 0xFF);
  out[7] = (uint8_t)((len >> 24) & 0xFF);
}

char link_reply_type(char t) { return (t >= 'A' && t <= 'Z') ? (char)(t - 'A' + 'a') : t; }

static bool is_letter(uint8_t c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'); }

static const uint8_t MAGIC[3] = {LINK_MAGIC0, LINK_MAGIC1, LINK_MAGIC2};

LinkDecoder::LinkDecoder(uint32_t max_payload)
    : state_(SCAN), magic_(0), hdr_{0, 0, 0, 0, 0}, hdr_n_(0), type_(0), accepted_(false), len_(0), left_(0),
      max_(max_payload), last_ms_(0), frames_(0), dropped_(0) {}

void LinkDecoder::reset(LinkSink *sink) {
  if (state_ == PAYLOAD && sink) sink->frame_abort(type_);
  state_ = SCAN;
  magic_ = 0;
  hdr_n_ = 0;
  left_ = 0;
}

uint32_t LinkDecoder::need() const {
  if (state_ == SCAN) return LINK_HEADER_LEN - magic_; /* the shortest frame is a bare header */
  if (state_ == HEADER) return (uint32_t)(sizeof hdr_ - hdr_n_);
  return left_;
}

bool LinkDecoder::check_timeout(uint32_t now_ms, uint32_t timeout_ms, LinkSink &sink) {
  if (state_ == SCAN || (uint32_t)(now_ms - last_ms_) < timeout_ms) return false;
  dropped_++;
  reset(&sink);
  return true;
}

size_t LinkDecoder::feed(const uint8_t *data, size_t n, LinkSink &sink, uint32_t now_ms) {
  size_t i = 0;
  if (n) last_ms_ = now_ms;
  while (i < n) {
    if (state_ == SCAN) {
      uint8_t c = data[i++];
      if (c == MAGIC[magic_]) {
        if (++magic_ == 3) {
          state_ = HEADER;
          hdr_n_ = 0;
          magic_ = 0;
        }
      } else {
        magic_ = (c == MAGIC[0]) ? 1 : 0;
      }
      continue;
    }
    if (state_ == HEADER) {
      hdr_[hdr_n_++] = data[i++];
      if (hdr_n_ < sizeof hdr_) continue;
      uint32_t len = (uint32_t)hdr_[1] | ((uint32_t)hdr_[2] << 8) | ((uint32_t)hdr_[3] << 16) |
                     ((uint32_t)hdr_[4] << 24);
      if (!is_letter(hdr_[0]) || len > max_) {
        /* Not a frame (noise that happened to contain MAGIC): rescan. */
        dropped_++;
        state_ = SCAN;
        continue;
      }
      type_ = (char)hdr_[0];
      len_ = left_ = len;
      accepted_ = sink.frame_begin(type_, len_);
      state_ = PAYLOAD;
    }
    /* PAYLOAD (possibly empty) */
    size_t take = n - i < left_ ? n - i : left_;
    if (take) {
      if (accepted_) sink.frame_data(data + i, take);
      i += take;
      left_ -= (uint32_t)take;
    }
    if (left_ == 0) {
      state_ = SCAN;
      frames_++;
      sink.frame_end(type_, accepted_);
      return i;
    }
  }
  return i;
}
