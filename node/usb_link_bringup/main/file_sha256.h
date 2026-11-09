#pragma once
/* SHA-256 of a file as 64 lowercase hex characters, the same format as
 * Python's hashlib hexdigest() on the Hub. */

#include "esp_err.h"

#define FILE_SHA256_HEX_LEN 64

/* out_hex must hold FILE_SHA256_HEX_LEN + 1 bytes. Not reentrant: uses one
 * static 4 KB read buffer. Only export_fsm calls it. */
esp_err_t file_sha256_hex(const char *path, char out_hex[FILE_SHA256_HEX_LEN + 1]);
