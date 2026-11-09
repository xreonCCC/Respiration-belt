/*
 * Streams a file through mbedTLS SHA-256 in 4 KB chunks, since recordings are
 * tens of MB. mbedTLS uses the S3's SHA hardware
 * (CONFIG_MBEDTLS_HARDWARE_SHA),
 * so the SD read speed is the limit.
 */

#include "file_sha256.h"

#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

#define CHUNK_BYTES 4096
#define YIELD_EVERY_CHUNKS 64 /* every 256 KB, let lower-priority tasks run */

esp_err_t file_sha256_hex(const char *path, char out_hex[FILE_SHA256_HEX_LEN + 1]) {
    static uint8_t chunk[CHUNK_BYTES]; /* static: 4 KB is too much for a task stack */
    static const char HEX[] = "0123456789abcdef";

    FILE *file = fopen(path, "rb");
    if (file == NULL) {
        return ESP_ERR_NOT_FOUND;
    }

    mbedtls_sha256_context ctx;
    mbedtls_sha256_init(&ctx);
    int rc = mbedtls_sha256_starts(&ctx, 0 /* 0 = SHA-256, 1 = SHA-224 */);

    size_t got;
    unsigned chunks = 0;
    while (rc == 0 && (got = fread(chunk, 1, sizeof(chunk), file)) > 0) {
        rc = mbedtls_sha256_update(&ctx, chunk, got);
        if (++chunks % YIELD_EVERY_CHUNKS == 0) {
            /* Hashing a large file takes seconds. The delay lets the idle
             * task run so the task watchdog does not fire. */
            vTaskDelay(1);
        }
    }
    const bool read_error = ferror(file) != 0;
    fclose(file);

    uint8_t digest[32];
    if (rc == 0) {
        rc = mbedtls_sha256_finish(&ctx, digest);
    }
    mbedtls_sha256_free(&ctx);
    if (rc != 0 || read_error) {
        return ESP_FAIL;
    }

    for (int i = 0; i < 32; ++i) {
        out_hex[2 * i] = HEX[digest[i] >> 4];
        out_hex[2 * i + 1] = HEX[digest[i] & 0x0F];
    }
    out_hex[FILE_SHA256_HEX_LEN] = '\0';
    return ESP_OK;
}
