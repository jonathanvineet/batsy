#!/bin/sh
# Build the Batsy widgets app and put it where Batsy serves it from (android/dist/batsy-widgets.apk).
# Needs a JDK (17+) and the Android SDK (ANDROID_HOME or ~/Library/Android/sdk / ~/Android/Sdk).
set -e
cd "$(dirname "$0")"
if [ -z "$ANDROID_HOME" ]; then
  for d in "$HOME/Library/Android/sdk" "$HOME/Android/Sdk"; do [ -d "$d" ] && export ANDROID_HOME="$d"; done
fi
echo "sdk.dir=$ANDROID_HOME" > local.properties
if [ -x ./gradlew ]; then G=./gradlew; else G=gradle; fi
$G --quiet --no-daemon :app:assembleRelease  # no build server left running afterwards (it holds ~1.4 GB)
mkdir -p dist
cp app/build/outputs/apk/release/app-release.apk dist/batsy-widgets.apk
echo "Built dist/batsy-widgets.apk ($(du -h dist/batsy-widgets.apk | cut -f1))"
