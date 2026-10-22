/* Host-test stub of ESP-IDF's heap_caps: records every live allocation so tests
 * can check sizes, leaks and overruns. Each block gets 64 KB of slack so an
 * overrun under test is detected by the size check instead of corrupting the
 * host heap. */
#pragma once
#include <cstddef>
#include <cstdlib>
#include <map>

#define MALLOC_CAP_SPIRAM (1 << 10)

namespace stub_heap {
inline std::map<void *, size_t> live;
inline size_t live_bytes = 0;
inline int fail_allocs = 0; /* > 0: the next N allocations fail */
inline size_t size_of(const void *p) {
  auto it = live.find(const_cast<void *>(p));
  return it == live.end() ? 0 : it->second;
}
}  // namespace stub_heap

inline void *heap_caps_malloc(size_t n, unsigned) {
  if (stub_heap::fail_allocs > 0) {
    stub_heap::fail_allocs--;
    return nullptr;
  }
  void *p = std::malloc(n + 64 * 1024);
  stub_heap::live[p] = n;
  stub_heap::live_bytes += n;
  return p;
}

inline void heap_caps_free(void *p) {
  if (!p) return;
  auto it = stub_heap::live.find(p);
  if (it != stub_heap::live.end()) {
    stub_heap::live_bytes -= it->second;
    stub_heap::live.erase(it);
  }
  std::free(p);
}
