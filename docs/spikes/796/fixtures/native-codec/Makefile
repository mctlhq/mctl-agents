CC ?= cc
CFLAGS = -O2 -fno-ident -Wall
codec: codec.c
	$(CC) $(CFLAGS) -o $@ codec.c
	strip -s $@
