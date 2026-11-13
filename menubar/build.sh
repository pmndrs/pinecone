#!/bin/zsh
# Build Pinecone.app into ./build (needs only the Xcode Command Line Tools).
set -e
cd "$(dirname "$0")"
swift build -c release
APP=build/Pinecone.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/Pinecone "$APP/Contents/MacOS/Pinecone"
cp Info.plist "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
echo "built $APP"
