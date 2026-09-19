#!/usr/bin/env bash
# Build stable-diffusion.cpp inside the rootfs cache: the same ggml pattern as the
# language runtime — every x86 CPU variant plus the Vulkan backend, loaded
# dynamically — so image models run on any GPU and on the CPU when there is none.
# It keeps its own ggml (a fork pinned by the project) in its own prefix, so the
# two engines never load each other's backends.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD/build/rootfs"
APT=(-o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)
source build/versions.env
STAMP="$SD_COMMIT vulkan cpu-all"
if [[ -f "$ROOT/opt/aios/imaging/COMMIT" && $(cat "$ROOT/opt/aios/imaging/COMMIT") == "$STAMP" && -f "$ROOT/opt/aios/imaging/lib/libggml-vulkan.so" ]]; then
  exit 0
fi
chroot "$ROOT" apt-get "${APT[@]}" update
chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends glslc libvulkan-dev spirv-headers
[[ -f build/sd.tar.gz ]] || curl --fail --location --retry 3 "https://github.com/leejet/stable-diffusion.cpp/archive/$SD_COMMIT.tar.gz" -o build/sd.tar.gz
# GitHub archives never contain submodules; ggml is a fork pinned by that commit.
[[ -f build/sd-ggml.tar.gz ]] || curl --fail --location --retry 3 "https://github.com/leejet/ggml/archive/$SD_GGML_COMMIT.tar.gz" -o build/sd-ggml.tar.gz
SOURCE="$ROOT/usr/src/stable-diffusion.cpp-$SD_COMMIT"
if [[ $(cat "$ROOT/usr/src/sd-build/AIOS_STAMP" 2>/dev/null) != "$STAMP" ]]; then
  rm -rf "$SOURCE" "$ROOT/usr/src/sd-build"
  tar -xzf build/sd.tar.gz -C "$ROOT/usr/src"
  rm -rf "$SOURCE/ggml"
  tar -xzf build/sd-ggml.tar.gz -C "$SOURCE"
  mv "$SOURCE/ggml-$SD_GGML_COMMIT" "$SOURCE/ggml"
  # The web UI needs pnpm and a second frontend repository, and AIOS has its own
  # portal; WebP and WebM would need two more submodules for formats nobody asks for.
  chroot "$ROOT" cmake -S "/usr/src/stable-diffusion.cpp-$SD_COMMIT" -B /usr/src/sd-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DSD_VULKAN=ON -DSD_BUILD_EXAMPLES=ON -DSD_SERVER_BUILD_FRONTEND=OFF -DSD_WEBP=OFF -DSD_WEBM=OFF \
    -DSD_BUILD_SHARED_GGML_LIB=ON \
    -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DBUILD_SHARED_LIBS=ON \
    -DCMAKE_INSTALL_PREFIX=/opt/aios/imaging -DCMAKE_INSTALL_RPATH=/opt/aios/imaging/lib
  echo "$STAMP" > "$ROOT/usr/src/sd-build/AIOS_STAMP"
fi
chroot "$ROOT" cmake --build /usr/src/sd-build --target sd-server -j2 ||
  chroot "$ROOT" cmake --build /usr/src/sd-build --target sd-server -j1
rm -rf "$ROOT/opt/aios/imaging"
mkdir -p "$ROOT/opt/aios/imaging/bin" "$ROOT/opt/aios/imaging/lib"
cp "$ROOT/usr/src/sd-build/bin/sd-server" "$ROOT/opt/aios/imaging/bin/"
cp -a "$ROOT"/usr/src/sd-build/bin/*.so* "$ROOT/opt/aios/imaging/lib/"
[[ -f "$ROOT/opt/aios/imaging/lib/libggml-vulkan.so" ]] || { echo 'Vulkan backend missing from the imaging build'; exit 1; }
# ggml looks for its backends next to the executable (see scripts/build-image.sh).
for backend in "$ROOT"/opt/aios/imaging/lib/libggml-cpu*.so "$ROOT"/opt/aios/imaging/lib/libggml-vulkan.so; do
  ln -sf "../lib/$(basename "$backend")" "$ROOT/opt/aios/imaging/bin/$(basename "$backend")"
done
echo "$STAMP" > "$ROOT/opt/aios/imaging/COMMIT"
