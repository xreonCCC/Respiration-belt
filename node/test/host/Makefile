# Parser tests on the host (gcc or clang): make -C node/test/host
CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -g -fsanitize=address,undefined
MAIN := ../../usb_link_bringup/main

test: test_node_protocol
	./test_node_protocol

test_node_protocol: test_node_protocol.c $(MAIN)/node_protocol.c $(MAIN)/node_protocol.h
	$(CC) $(CFLAGS) -I$(MAIN) test_node_protocol.c $(MAIN)/node_protocol.c -o $@

clean:
	rm -f test_node_protocol

.PHONY: test clean
