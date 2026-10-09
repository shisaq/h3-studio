#!/bin/bash
# Build the helper used by the macos_translate plugin (needs macOS 15+ and the Xcode command line tools).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p bin
swiftc -O -parse-as-library tools/translate.swift -o bin/h3-translate
echo "Built bin/h3-translate"
echo "Language packs: $(bin/h3-translate --status zh-Hans en)"
echo "If that does not say \"installed\": System Settings > General > Language & Region > Translation Languages,"
echo "then download Chinese (Simplified) and English."
