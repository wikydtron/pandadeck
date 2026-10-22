/* Transport-independent request handling, shared by the HTTP server
 * (net.cpp, token required) and the USB link (usb_link.cpp, physical access
 * = trusted). Both transports handle requests in the same task (net.cpp's
 * link task), so these never race each other; the one exception is the
 * image staging, which the USB link fills from its receive callback (see
 * api_image_*). Replies are JSON written into the caller's buffer; `status`
 * is an HTTP status code the USB link ignores (its JSON says ok / error). */
#pragma once
#include "proto.h"

#include <stddef.h>
#include <stdint.h>

enum ApiOwner : uint8_t { API_NONE = 0, API_HTTP, API_USB };

struct ApiResult {
  int status;
  size_t len;
};

/* Panel info. USB (`h` reply) includes the token so the app can use Wi-Fi
 * later; HTTP (`GET /api/info`, no token needed) adds diagnostics +
 * partitions and only shows the Wi-Fi SSID to a caller that sent the token.
 * nonce (X-Panda-Nonce, 8..128 hex chars) adds "proof":
 * hex(HMAC-SHA256(key = token, msg = the nonce's ASCII text)), so the app
 * can check it found the right panel before it sends the token there. */
ApiResult api_info(char *out, size_t cap, ApiOwner via, bool authed, const char *nonce = nullptr);
/* Snapshot in (`S` / POST /api/sync), events out. Empty body = poll. */
ApiResult api_sync(const char *json, size_t len, char *out, size_t cap);
/* Settings update (`C` / POST /api/config). Disruptive parts (Wi-Fi
 * reconnect, reboots) are deferred: call api_after_reply() once the reply
 * is out. */
ApiResult api_config(const char *json, size_t len, char *out, size_t cap);

/* Images (`I` / POST /api/icon, /api/backdrop) streamed into the icon /
 * backdrop staging buffers. One upload at a time across both transports:
 * begin returns "busy" while the other one owns staging. Errors are short
 * strings, nullptr = ok. */
const char *api_image_begin(ApiOwner who, const ImageHeader &h);
void api_image_write(ApiOwner who, const uint8_t *data, size_t n);
const char *api_image_commit(ApiOwner who);
void api_image_abort(ApiOwner who);

/* Firmware update (`U`/`D` frames, POST /update). size 0 = unknown (HTTP
 * multipart). end() verifies (MD5 if given) and schedules the reboot. */
const char *api_update_begin(ApiOwner who, uint32_t size, const char *md5);
const char *api_update_write(ApiOwner who, const uint8_t *data, size_t n);
const char *api_update_end(ApiOwner who);
void api_update_abort(ApiOwner who);
bool api_update_running();
uint32_t api_update_written();

/* Runs what a request deferred until its reply was sent (Wi-Fi reconnect,
 * reboot, reboot into the ROM bootloader). Does not return on a reboot. */
void api_after_reply();
/* {"ok":false,"error":"..."} helper. */
ApiResult api_error(char *out, size_t cap, int status, const char *err);
