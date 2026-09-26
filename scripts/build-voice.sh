#!/usr/bin/env bash
# Build whisper.cpp inside the rootfs cache: the same ggml pattern as the other
# two engines — every x86 CPU variant plus the Vulkan backend, loaded
# dynamically — so speech models transcribe on any GPU and on the CPU when there
# is none. Its own prefix keeps its ggml apart from the language and image ones.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD/build/rootfs"
APT=(-o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)
source build/versions.env
STAMP="$WHISPER_COMMIT vulkan cpu-all"
if [[ -f "$ROOT/opt/aios/voice/COMMIT" && $(cat "$ROOT/opt/aios/voice/COMMIT") == "$STAMP" && -f "$ROOT/opt/aios/voice/lib/libggml-vulkan.so" ]]; then
  exit 0
fi
chroot "$ROOT" apt-get "${APT[@]}" update
chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends glslc libvulkan-dev spirv-headers
[[ -f build/whisper.tar.gz ]] || curl --fail --location --retry 3 "https://github.com/ggml-org/whisper.cpp/archive/$WHISPER_COMMIT.tar.gz" -o build/whisper.tar.gz
SOURCE="$ROOT/usr/src/whisper.cpp-$WHISPER_COMMIT"
if [[ $(cat "$ROOT/usr/src/whisper-build/AIOS_STAMP" 2>/dev/null) != "$STAMP" ]]; then
  rm -rf "$SOURCE" "$ROOT/usr/src/whisper-build"
  tar -xzf build/whisper.tar.gz -C "$ROOT/usr/src"
  # The server is the only example the appliance runs; ffmpeg decoding is done by
  # the server itself through the ffmpeg binary already in the image.
  chroot "$ROOT" cmake -S "/usr/src/whisper.cpp-$WHISPER_COMMIT" -B /usr/src/whisper-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_SERVER=ON -DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF -DWHISPER_CURL=OFF \
    -DGGML_VULKAN=ON -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DBUILD_SHARED_LIBS=ON \
    -DCMAKE_INSTALL_PREFIX=/opt/aios/voice -DCMAKE_INSTALL_RPATH=/opt/aios/voice/lib
  echo "$STAMP" > "$ROOT/usr/src/whisper-build/AIOS_STAMP"
fi
chroot "$ROOT" cmake --build /usr/src/whisper-build --target whisper-server -j2 ||
  chroot "$ROOT" cmake --build /usr/src/whisper-build --target whisper-server -j1
rm -rf "$ROOT/opt/aios/voice"
mkdir -p "$ROOT/opt/aios/voice/bin" "$ROOT/opt/aios/voice/lib"
cp "$ROOT/usr/src/whisper-build/bin/whisper-server" "$ROOT/opt/aios/voice/bin/"
cp -a "$ROOT"/usr/src/whisper-build/bin/*.so* "$ROOT/opt/aios/voice/lib/"
[[ -f "$ROOT/opt/aios/voice/lib/libggml-vulkan.so" ]] || { echo 'Vulkan backend missing from the voice build'; exit 1; }
# ggml looks for its backends next to the executable (see scripts/build-image.sh).
for backend in "$ROOT"/opt/aios/voice/lib/libggml-cpu*.so "$ROOT"/opt/aios/voice/lib/libggml-vulkan.so; do
  ln -sf "../lib/$(basename "$backend")" "$ROOT/opt/aios/voice/bin/$(basename "$backend")"
done
chroot "$ROOT" /opt/aios/voice/bin/whisper-server --help > /dev/null
echo "$STAMP" > "$ROOT/opt/aios/voice/COMMIT"
echo "Built whisper.cpp $WHISPER_COMMIT in /opt/aios/voice"
