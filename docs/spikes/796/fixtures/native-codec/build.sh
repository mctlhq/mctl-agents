#!/bin/sh
# Rebuild the fixture and print its sha256. The digest depends on the
# toolchain: record it together with the compiler version in the results.
set -eu
cd "$(dirname "$0")"
make -B -s codec >&2
sha256sum codec | cut -d' ' -f1
