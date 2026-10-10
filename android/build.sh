#!/usr/bin/env bash
# Builds the Sentinel Live Android shell.
#
# No gradle, no third-party libraries: aapt2 / javac / d8 / zipalign /
# apksigner from the android sdk build-tools. The sdk binaries are x86_64 and
# this phone is aarch64, so aapt2 and zipalign go through the qemu wrappers
# (see /workspace/kingstrade/tools/bin).
#
#   bash build.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/app"
OUT="$HERE/build"

SDK=${SDK:-/tmp/opencode/android/sdk}
BT=$SDK/build-tools/34.0.0
PLATFORM=$SDK/platforms/android-34/android.jar
JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk}
export JAVA_HOME
export PATH="$JAVA_HOME/bin:/workspace/kingstrade/tools/bin:/usr/bin:/bin"

AAPT2=${AAPT2:-/workspace/kingstrade/tools/bin/aapt2}
ZIPALIGN=${ZIPALIGN:-/workspace/kingstrade/tools/bin/zipalign}
D8=$BT/d8
APKSIGNER=$BT/apksigner

KS=${KS:-$HERE/sentinel-release.jks}
KS_ALIAS=${KS_ALIAS:-sentinel-release}
KSPASS=${KSPASS:-sentinel-release}
KS_KEYPASS=${KS_KEYPASS:-$KSPASS}

VERSION_CODE=${VERSION_CODE:-1}
VERSION_NAME=${VERSION_NAME:-1.0.0}

for f in "$AAPT2" "$ZIPALIGN" "$D8" "$APKSIGNER" "$PLATFORM"; do
  [ -e "$f" ] || { echo "missing tool: $f"; exit 1; }
done

echo "== icons =="
python3 "$HERE/tools/gen_icons.py"

rm -rf "$OUT"
mkdir -p "$OUT/compiled" "$OUT/gen" "$OUT/classes"

echo "== aapt2 compile =="
"$AAPT2" compile --dir "$APP/res" -o "$OUT/compiled/res.zip"

echo "== aapt2 link =="
"$AAPT2" link \
  -o "$OUT/app-unsigned.apk" \
  -I "$PLATFORM" \
  --manifest "$APP/AndroidManifest.xml" \
  --java "$OUT/gen" \
  --min-sdk-version 24 --target-sdk-version 34 \
  --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
  "$OUT/compiled/res.zip"

echo "== javac =="
find "$APP/java" "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
"$JAVA_HOME/bin/javac" -source 8 -target 8 -nowarn -encoding UTF-8 \
  -classpath "$PLATFORM" \
  -d "$OUT/classes" @"$OUT/sources.txt" 2> "$OUT/javac.log" || {
    echo "javac failed:"; cat "$OUT/javac.log"; exit 1; }

echo "== d8 =="
find "$OUT/classes" -name '*.class' > "$OUT/classes.txt"
"$D8" --release --min-api 24 --lib "$PLATFORM" --output "$OUT" @"$OUT/classes.txt"

echo "== package =="
cp "$OUT/app-unsigned.apk" "$OUT/app-withdex.apk"
( cd "$OUT" && zip -q -j app-withdex.apk classes.dex )

echo "== zipalign =="
"$ZIPALIGN" -f -p 4 "$OUT/app-withdex.apk" "$OUT/app-aligned.apk"

echo "== signing =="
if [ ! -f "$KS" ]; then
  echo "creating release keystore: $KS"
  "$JAVA_HOME/bin/keytool" -genkeypair -v -keystore "$KS" \
    -alias "$KS_ALIAS" -keyalg RSA -keysize 4096 -validity 10950 \
    -storetype PKCS12 \
    -storepass "$KSPASS" -keypass "$KS_KEYPASS" \
    -dname "CN=Sentinel Live, OU=Mobile, O=Sentinel, L=Dhaka, C=BD" >/dev/null 2>&1
fi
# v1 for API 24-25, v2 for 7+, v3 for future key rotation
"$APKSIGNER" sign --ks "$KS" --ks-pass "pass:$KSPASS" --key-pass "pass:$KS_KEYPASS" \
  --ks-key-alias "$KS_ALIAS" \
  --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
  --out "$OUT/sentinel-live.apk" "$OUT/app-aligned.apk"

echo "== verify =="
"$APKSIGNER" verify --verbose "$OUT/sentinel-live.apk" | grep -E "^Verif" | head -5
"$AAPT2" dump badging "$OUT/sentinel-live.apk" 2>/dev/null \
  | grep -E "^package|launchable-activity|uses-permission" | head -6

SIZE=$(wc -c < "$OUT/sentinel-live.apk")
echo "APK: $OUT/sentinel-live.apk  $SIZE bytes"
echo "SHA-256: $(sha256sum "$OUT/sentinel-live.apk" | cut -d' ' -f1)"