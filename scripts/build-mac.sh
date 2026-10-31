#!/usr/bin/env bash
# Build the release "Handy Personal.app" for Apple Silicon, signed with a
# stable identity so macOS privacy grants (Microphone, Accessibility, Input
# Monitoring) survive rebuilds.
#
# Usage:
#   export APPLE_SIGNING_IDENTITY="Apple Development: Your Name (TEAMID)"
#   scripts/build-mac.sh            # build only
#   scripts/build-mac.sh --install  # build, then copy to /Applications
#
# List the identities available in your keychain with:
#   security find-identity -v -p codesigning
#
# Without APPLE_SIGNING_IDENTITY the app is signed ad-hoc ("-"). Ad-hoc
# signatures change on every build, so macOS treats each rebuild as a new app
# and you have to re-grant Accessibility each time.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    -h | --help)
      sed -n '2,17p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: scripts/build-mac.sh must run on macOS" >&2
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "warning: this script is tuned for Apple Silicon; continuing on $(uname -m)" >&2
fi

for tool in bun cargo codesign; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: '$tool' not found on PATH (see BUILD.md prerequisites)" >&2
    exit 1
  fi
done

APP_NAME="Handy Personal"
BUNDLE_ID="$(sed -n 's/^  "identifier": "\(.*\)",$/\1/p' src-tauri/tauri.conf.json)"

if [[ -z "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  cat >&2 <<'WARN'

################################################################################
#                                                                              #
#   WARNING: APPLE_SIGNING_IDENTITY is not set -- signing AD-HOC.              #
#                                                                              #
#   macOS ties Accessibility / Input Monitoring grants to the code signature.  #
#   An ad-hoc signature changes on every build, so after each rebuild you      #
#   will have to remove and re-add "Handy Personal" in System Settings >       #
#   Privacy & Security. Set APPLE_SIGNING_IDENTITY to your Apple Development   #
#   certificate to avoid this:                                                 #
#                                                                              #
#     security find-identity -v -p codesigning                                 #
#     export APPLE_SIGNING_IDENTITY="Apple Development: Name (TEAMID)"         #
#                                                                              #
################################################################################

WARN
  export APPLE_SIGNING_IDENTITY="-"
  sleep 3
else
  if ! security find-identity -v -p codesigning | grep -Fq "$APPLE_SIGNING_IDENTITY"; then
    echo "error: signing identity not found in keychain: $APPLE_SIGNING_IDENTITY" >&2
    echo "Available identities:" >&2
    security find-identity -v -p codesigning >&2 || true
    exit 1
  fi
  echo "Signing with: $APPLE_SIGNING_IDENTITY"
fi

# Notarization is for distribution; a personal build never needs it. Make sure
# stray Apple ID credentials in the environment don't trigger it.
unset APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID APPLE_API_KEY APPLE_API_ISSUER APPLE_API_KEY_PATH || true
# No updater artifacts are produced (createUpdaterArtifacts is false), so no
# TAURI_SIGNING_PRIVATE_KEY is needed.

VAD_MODEL="src-tauri/resources/models/silero-vad-v6.2-F32.gguf"
if [[ ! -s "$VAD_MODEL" ]]; then
  echo "Downloading the Silero VAD model..."
  mkdir -p "$(dirname "$VAD_MODEL")"
  curl -fL -o "$VAD_MODEL" \
    https://huggingface.co/handy-computer/silero-vad-v6.2-gguf/resolve/main/silero-vad-v6.2-F32.gguf
fi

bun install --frozen-lockfile

# Some CMake versions reject the old policy minimums in vendored C++ deps.
export CMAKE_POLICY_VERSION_MINIMUM="${CMAKE_POLICY_VERSION_MINIMUM:-3.5}"

# Tauri reads APPLE_SIGNING_IDENTITY and overrides bundle.macOS.signingIdentity.
bun run tauri build --bundles app

APP_PATH="src-tauri/target/release/bundle/macos/${APP_NAME}.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "error: expected bundle not found at $APP_PATH" >&2
  exit 1
fi

echo
echo "Verifying signature..."
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
codesign -dv --verbose=2 "$APP_PATH" 2>&1 | grep -E "^(Identifier|Authority|TeamIdentifier|Signature)=" || true
echo "Designated requirement:"
codesign -d -r- "$APP_PATH" 2>&1 | sed -n 's/^designated => /  /p'

if [[ "$APPLE_SIGNING_IDENTITY" == "-" ]]; then
  echo
  echo "Built AD-HOC signed app (permissions will need re-granting after rebuilds)."
fi

if [[ "$INSTALL" == "1" ]]; then
  DEST="/Applications/${APP_NAME}.app"
  echo
  echo "Installing to $DEST ..."
  osascript -e "tell application id \"${BUNDLE_ID}\" to quit" >/dev/null 2>&1 || true
  sleep 1
  rm -rf "$DEST"
  ditto "$APP_PATH" "$DEST"
  echo "Installed. Launch with: open \"$DEST\""
else
  echo
  echo "Built: $APP_PATH"
  echo "Install with: scripts/build-mac.sh --install   (or drag it to /Applications)"
fi
