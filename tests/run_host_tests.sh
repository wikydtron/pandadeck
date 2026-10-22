#!/usr/bin/env bash
# Host-side tests: the firmware's pure modules (C++) + the desktop app (Python).
set -euo pipefail
cd "$(dirname "$0")/.."
GXX="${GXX:-g++}"
FW=firmware/PandaDeck
CXXFLAGS=(-std=c++17 -O1 -Wall -Wextra -I "$FW" -I firmware/libraries/ArduinoJson/src)
"$GXX" "${CXXFLAGS[@]}" tests/model_tests.cpp "$FW/model.cpp" "$FW/deck_action.cpp" -o tests/model_tests.exe
./tests/model_tests.exe tests/fixtures/snapshot.json
"$GXX" "${CXXFLAGS[@]}" tests/link_tests.cpp "$FW/link_codec.cpp" "$FW/proto.cpp" -o tests/link_tests.exe
./tests/link_tests.exe
"$GXX" "${CXXFLAGS[@]}" -I tests/stubs tests/icon_store_tests.cpp "$FW/icon_store.cpp" -o tests/icon_store_tests.exe
./tests/icon_store_tests.exe
python -m pytest -q tests
