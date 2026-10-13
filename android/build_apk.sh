#!/bin/bash
set -e

ANDROID_JAR="/tmp/opencode/android/sdk/platforms/android-34/android.jar"
BUILD_DIR="/workspace/sentinel/android/build"
SRC_DIR="/workspace/sentinel/android/app/src/main"
RES_DIR="$SRC_DIR/res"
JAVA_DIR="$SRC_DIR/java"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/classes" "$BUILD_DIR/apk" "$BUILD_DIR/gen"

find "$JAVA_DIR" -name "*.java" > "$BUILD_DIR/sources.txt"

javac -source 1.8 -target 1.8 \
    -cp "$ANDROID_JAR" \
    -d "$BUILD_DIR/classes" \
    @"$BUILD_DIR/sources.txt" 2>&1 | head -50

aapt2 compile --dir "$RES_DIR" -o "$BUILD_DIR/resources.zip"

aapt2 link \
    --proto-format \
    -o "$BUILD_DIR/apk/sentinel-unsigned.apk" \
    -I "$ANDROID_JAR" \
    --manifest "$SRC_DIR/AndroidManifest.xml" \
    --java "$BUILD_DIR/gen" \
    "$BUILD_DIR/resources.zip" 2>&1 | head -30

cd "$BUILD_DIR/classes" && jar uf "$BUILD_DIR/apk/sentinel-unsigned.apk" . && cd /workspace/sentinel

zipalign -f 4 "$BUILD_DIR/apk/sentinel-unsigned.apk" "$BUILD_DIR/apk/sentinel-aligned.apk"

apksigner sign \
    --ks /workspace/sentinel/android/sentinel-release.jks \
    --ks-pass pass:sentinel \
    --key-pass pass:sentinel \
    --out "$BUILD_DIR/sentinel-live.apk" \
    "$BUILD_DIR/apk/sentinel-aligned.apk" 2>&1

echo "APK built: $BUILD_DIR/sentinel-live.apk"
ls -la "$BUILD_DIR/sentinel-live.apk"
