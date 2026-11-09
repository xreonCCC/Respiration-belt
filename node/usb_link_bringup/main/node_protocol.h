#pragma once
/*
 * CDC command protocol between Hub and Node.
 *
 * hub/hub_node_protocol.py mirrors every format in this file; change both
 * sides together.
 *
 * - ASCII, one command per line, terminated by '\n' ('\r' is ignored).
 * - The Hub always speaks first and gets exactly one reply line per command.
 *   Logs go to UART0 only, never to this port.
 * - Lines longer than NODE_PROTO_MAX_LINE are discarded.
 */

#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define NODE_PROTO_MAX_LINE 128      /* longest accepted command, without '\n' */
#define NODE_PROTO_MAX_FILENAME 32   /* FAT 8.3 names fit easily */
#define NODE_PROTO_SHA256_HEX_LEN 64 /* 32 bytes as lowercase hex */

/* 2024-01-01 UTC */
#define NODE_PROTO_MIN_EPOCH 1704067200LL

/* ---- Commands (Hub -> Node) ---- */

typedef enum {
    NODE_CMD_INVALID = 0,
    NODE_CMD_PING,
    NODE_CMD_RTCSET,
    NODE_CMD_RTCGET,
    NODE_CMD_BATT,
    NODE_CMD_DELETE_OK,
    NODE_CMD_DELETE_ABORT,
    NODE_CMD_STATUS
} node_cmd_type_t;

typedef struct {
    node_cmd_type_t type;
    int64_t epoch_s;                                 /* RTCSET only */
    char filename[NODE_PROTO_MAX_FILENAME + 1];      /* DELETE_OK, DELETE_ABORT */
    char sha256_hex[NODE_PROTO_SHA256_HEX_LEN + 1];  /* DELETE_OK, lowercase */
    const char *error; /* when INVALID: short static reason, e.g. "bad_filename" */
} node_cmd_t;

/* ---- Replies (Node -> Hub) ----
 * The first six follow the spec's examples. Replies marked PROPOSED are not
 * in the spec. */

#define NODE_REPLY_PONG "PONG"
#define NODE_REPLY_RTCSET_OK_FMT "RTCSET OK: epoch=%" PRId64
#define NODE_REPLY_RTC_STATUS_FMT "RTC STATUS: state=%s,epoch=%" PRId64
#define NODE_REPLY_BATT_FMT "BATT:PERCENT=%d,VOLTAGE_MV=%d,CHARGING=%d"
#define NODE_REPLY_DELETE_ACK_FMT "DELETE ACK: %s"
#define NODE_REPLY_DELETE_FAIL_FMT "DELETE FAIL: %s,reason=%s"

#define NODE_REPLY_DELETE_ABORTED_FMT "DELETE ABORTED: %s" /* PROPOSED */
#define NODE_REPLY_STATUS_FMT "STATUS: state=%s,owner=%s"  /* PROPOSED */
#define NODE_REPLY_ERR_FMT "ERR: %s"                       /* PROPOSED, for INVALID */

/* DELETE FAIL reasons. "not_found" is from the spec, the rest are PROPOSED. */
#define NODE_FAIL_NOT_FOUND "not_found"
#define NODE_FAIL_SHA_MISMATCH "sha_mismatch"
#define NODE_FAIL_WRONG_STATE "wrong_state"
#define NODE_FAIL_IO_ERROR "io_error"
#define NODE_FAIL_CARD_BUSY "card_busy"
#define NODE_FAIL_PROTECTED "protected" /* EVENTS.CSV is never deleted */

/* ---- Functions ---- */

/*
 * Parse one line (no '\n'; a trailing '\r' must already be stripped).
 * Fills *out and returns true for a valid command. On failure returns false,
 * sets out->type = NODE_CMD_INVALID and out->error to a short reason.
 * Never writes past the fixed-size buffers in node_cmd_t.
 */
bool node_proto_parse(const char *line, node_cmd_t *out);

/*
 * File names allowed: R[0-9A-Za-z_.-]+, T[0-9A-Za-z_.-]+
 * and EVENTS.CSV, further limited to NODE_PROTO_MAX_FILENAME characters and
 * no "..". Names only; the Node builds the path itself.
 */
bool node_proto_filename_allowed(const char *name);

/* Exactly 64 characters, all in [0-9a-f]. Uppercase is rejected. */
bool node_proto_sha256_hex_valid(const char *hex);
