#!/bin/sh
# macOS: double-click to build this bundle (Finder opens it in Terminal).
# First time: right-click > Open, because the file was downloaded.
cd "$(dirname "$0")" && exec sh ./build.sh "$@"
