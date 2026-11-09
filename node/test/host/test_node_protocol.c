/* Host tests for node_protocol.c: make -C node/test/host */

#include <stdio.h>
#include <string.h>

#include "node_protocol.h"

static int failures = 0;
static int checks = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        checks++;                                                       \
        if (!(cond)) {                                                  \
            failures++;                                                 \
            printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);      \
        }                                                               \
    } while (0)

static const char *SHA =
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";

static void test_simple_commands(void) {
    node_cmd_t c;
    CHECK(node_proto_parse("PING", &c) && c.type == NODE_CMD_PING);
    CHECK(node_proto_parse("RTCGET", &c) && c.type == NODE_CMD_RTCGET);
    CHECK(node_proto_parse("BATT?", &c) && c.type == NODE_CMD_BATT);
    CHECK(node_proto_parse("STATUS", &c) && c.type == NODE_CMD_STATUS);

    CHECK(!node_proto_parse("ping", &c));       /* case-sensitive */
    CHECK(!node_proto_parse("PING extra", &c)); /* no arguments allowed */
    CHECK(!node_proto_parse("", &c));
    CHECK(!node_proto_parse("HELLO", &c) && c.type == NODE_CMD_INVALID);
    CHECK(c.error != NULL);
}

static void test_rtcset(void) {
    node_cmd_t c;
    CHECK(node_proto_parse("RTCSET 1790000000", &c));
    CHECK(c.type == NODE_CMD_RTCSET && c.epoch_s == 1790000000LL);

    CHECK(!node_proto_parse("RTCSET", &c));
    CHECK(!node_proto_parse("RTCSET ", &c));
    CHECK(!node_proto_parse("RTCSET abc", &c));
    CHECK(!node_proto_parse("RTCSET 1790000000x", &c));
    CHECK(!node_proto_parse("RTCSET 1000", &c)); /* before 2024 */

    CHECK(!node_proto_parse("RTCSET +1790000000", &c)); /* no sign */
    CHECK(!node_proto_parse("RTCSET  1790000000", &c)); /* no extra space */
}

static void test_delete_ok(void) {
    node_cmd_t c;
    char line[160];

    snprintf(line, sizeof line, "DELETE_OK R0000007.CSV %s", SHA);
    CHECK(node_proto_parse(line, &c));
    CHECK(c.type == NODE_CMD_DELETE_OK);
    CHECK(strcmp(c.filename, "R0000007.CSV") == 0);
    CHECK(strcmp(c.sha256_hex, SHA) == 0);

    CHECK(!node_proto_parse("DELETE_OK R0000007.CSV", &c)); /* no hash */
    snprintf(line, sizeof line, "DELETE_OK ../R0000007.CSV %s", SHA);
    CHECK(!node_proto_parse(line, &c));
    snprintf(line, sizeof line, "DELETE_OK main.c %s", SHA);
    CHECK(!node_proto_parse(line, &c));
    CHECK(!node_proto_parse("DELETE_OK R0000007.CSV 1234", &c));

    /* 43 characters. Truncated to 32 it would still be a valid name, so it
     * must be rejected. */
    snprintf(line, sizeof line,
             "DELETE_OK R00000000000000000000000000000000000007.CSV %s", SHA);
    CHECK(!node_proto_parse(line, &c) && strcmp(c.error, "bad_filename") == 0);

    CHECK(node_proto_parse("DELETE_ABORT T0000005.CSV", &c));
    CHECK(c.type == NODE_CMD_DELETE_ABORT);
    CHECK(strcmp(c.filename, "T0000005.CSV") == 0);
}

static void test_filenames(void) {
    CHECK(node_proto_filename_allowed("R0000001.CSV"));
    CHECK(node_proto_filename_allowed("T0000005.CSV"));
    CHECK(node_proto_filename_allowed("EVENTS.CSV"));

    CHECK(!node_proto_filename_allowed(""));
    CHECK(!node_proto_filename_allowed("R"));           /* needs 1+ more chars */
    CHECK(!node_proto_filename_allowed("X0000001.CSV")); /* wrong prefix */
    CHECK(!node_proto_filename_allowed("R/../a"));
    CHECK(!node_proto_filename_allowed("R..CSV"));
    CHECK(!node_proto_filename_allowed("R0000001 .CSV"));
    CHECK(!node_proto_filename_allowed("events.csv"));
    CHECK(!node_proto_filename_allowed(
        "R000000000000000000000000000000000.CSV")); /* too long */
}

static void test_sha(void) {
    CHECK(node_proto_sha256_hex_valid(SHA));
    CHECK(!node_proto_sha256_hex_valid(""));
    CHECK(!node_proto_sha256_hex_valid(
        "9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08"));
    CHECK(!node_proto_sha256_hex_valid("9f86d0"));
}

static void test_long_line(void) {
    node_cmd_t c;
    char line[NODE_PROTO_MAX_LINE + 10];
    memset(line, 'A', sizeof line - 1);
    line[sizeof line - 1] = '\0';
    CHECK(!node_proto_parse(line, &c));
}

int main(void) {
    test_simple_commands();
    test_rtcset();
    test_delete_ok();
    test_filenames();
    test_sha();
    test_long_line();
    printf("%d/%d checks passed\n", checks - failures, checks);
    return failures == 0 ? 0 : 1;
}
