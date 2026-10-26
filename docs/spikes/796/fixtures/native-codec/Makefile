CC ?= cc
CFLAGS = -O2 -fno-ident -Wall
# GNU strip takes -s; BSD/macOS strip has no such flag and strips all symbols by default.
STRIP_FLAGS ?= $(if $(filter Darwin,$(shell uname -s)),,-s)
codec: codec.c
	$(CC) $(CFLAGS) -o $@ codec.c
	strip $(STRIP_FLAGS) $@
