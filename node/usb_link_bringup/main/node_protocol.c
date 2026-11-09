/*
 * Parser for commands from the Hub.
 *
 * A command is a bare keyword ("PING") or a keyword, one space and its
 * arguments. Input that does not match exactly is rejected; nothing is trimmed
 * or corrected, so a garbled DELETE_OK cannot name the wrong file.
 */

#include "node_protocol.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

static bool fail(node_cmd_t *out, const char *reason) {
    out->type = NODE_CMD_INVALID;
    out->error = reason;
    return false;
}

/* True when s starts with prefix. */
static bool starts_with(const char *s, const char *prefix) {
    return strncmp(s, prefix, strlen(prefix)) == 0;
}

/* Copy src into dst[dst_size] only if it fits completely (incl. the '\0').
 * Truncating a file name could make it refer to a different file. */
static bool copy_exact(char *dst, size_t dst_size, const char *src, size_t len) {
    if (len + 1 > dst_size) {
        return false;
    }
    memcpy(dst, src, len);
    dst[len] = '\0';
    return true;
}

bool node_proto_filename_allowed(const char *name) {
    if (name == NULL) {
        return false;
    }
    if (strcmp(name, "EVENTS.CSV") == 0) {
        return true;
    }
    const size_t len = strlen(name);
    if (len < 2 || len > NODE_PROTO_MAX_FILENAME) {
        return false;
    }
    if (name[0] != 'R' && name[0] != 'T') {
        return false;
    }
    if (strstr(name, "..") != NULL) {
        return false;
    }
    for (size_t i = 1; i < len; ++i) {
        const unsigned char c = (unsigned char)name[i];
        /* [0-9A-Za-z_.-] only ("C" locale isalnum), so no path separators. */
        if (!(isalnum(c) || c == '_' || c == '.' || c == '-')) {
            return false;
        }
    }
    return true;
}

bool node_proto_sha256_hex_valid(const char *hex) {
    if (hex == NULL || strlen(hex) != NODE_PROTO_SHA256_HEX_LEN) {
        return false;
    }
    for (size_t i = 0; i < NODE_PROTO_SHA256_HEX_LEN; ++i) {
        const char c = hex[i];
        const bool digit = c >= '0' && c <= '9';
        const bool lower_hex = c >= 'a' && c <= 'f';
        if (!digit && !lower_hex) {
            return false;
        }
    }
    return true;
}

/* "RTCSET " already matched; args points at the number. */
static bool parse_rtcset(const char *args, node_cmd_t *out) {
    /* strtoll() would also accept leading spaces and a sign. */
    if (!isdigit((unsigned char)args[0])) {
        return fail(out, "bad_epoch");
    }
    char *end = NULL;
    const long long value = strtoll(args, &end, 10);
    if (end == args || *end != '\0' || value < NODE_PROTO_MIN_EPOCH) {
        return fail(out, "bad_epoch");
    }
    out->type = NODE_CMD_RTCSET;
    out->epoch_s = (int64_t)value;
    return true;
}

/* "DELETE_OK " already matched; args is "<filename> <sha256>". */
static bool parse_delete_ok(const char *args, node_cmd_t *out) {
    const char *space = strchr(args, ' ');
    if (space == NULL) {
        return fail(out, "bad_args");
    }
    if (!copy_exact(out->filename, sizeof(out->filename), args, (size_t)(space - args)) ||
        !node_proto_filename_allowed(out->filename)) {
        return fail(out, "bad_filename");
    }
    const char *hash = space + 1;
    /* Extra arguments ("R1.CSV abc def") fail the hex check below. */
    if (!node_proto_sha256_hex_valid(hash)) {
        return fail(out, "bad_sha256");
    }
    memcpy(out->sha256_hex, hash, NODE_PROTO_SHA256_HEX_LEN + 1); /* includes '\0' */
    out->type = NODE_CMD_DELETE_OK;
    return true;
}

/* "DELETE_ABORT " already matched; args is "<filename>". */
static bool parse_delete_abort(const char *args, node_cmd_t *out) {
    if (!copy_exact(out->filename, sizeof(out->filename), args, strlen(args)) ||
        !node_proto_filename_allowed(out->filename)) {
        return fail(out, "bad_filename");
    }
    out->type = NODE_CMD_DELETE_ABORT;
    return true;
}

bool node_proto_parse(const char *line, node_cmd_t *out) {
    if (out == NULL) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    if (line == NULL) {
        return fail(out, "null");
    }
    /* Count at most MAX_LINE+1 characters, so even a missing '\0' can't make
     * us run off the end of the buffer. */
    size_t len = 0;
    while (len <= NODE_PROTO_MAX_LINE && line[len] != '\0') {
        ++len;
    }
    if (len > NODE_PROTO_MAX_LINE) {
        return fail(out, "too_long");
    }

    /* Commands without arguments: whole-line match. */
    static const struct {
        const char *text;
        node_cmd_type_t type;
    } exact[] = {
        {"PING", NODE_CMD_PING},
        {"RTCGET", NODE_CMD_RTCGET},
        {"BATT?", NODE_CMD_BATT},
        {"STATUS", NODE_CMD_STATUS},
    };
    for (size_t i = 0; i < sizeof(exact) / sizeof(exact[0]); ++i) {
        if (strcmp(line, exact[i].text) == 0) {
            out->type = exact[i].type;
            return true;
        }
    }

    /* Commands with arguments: "KEYWORD " prefix, then the arguments. */
    if (starts_with(line, "RTCSET ")) {
        return parse_rtcset(line + strlen("RTCSET "), out);
    }
    if (starts_with(line, "DELETE_OK ")) {
        return parse_delete_ok(line + strlen("DELETE_OK "), out);
    }
    if (starts_with(line, "DELETE_ABORT ")) {
        return parse_delete_abort(line + strlen("DELETE_ABORT "), out);
    }
    return fail(out, "unknown_command");
}
