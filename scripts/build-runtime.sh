#!/usr/bin/env bash
# Build llama.cpp inside the rootfs cache: every x86 CPU variant plus the Vulkan
# backend. Both are dynamic backends, so a machine without a usable GPU simply
# never loads the Vulkan one and runs on the CPU as before.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD/build/rootfs"
APT=(-o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)
source build/versions.env
STAMP="$LLAMA_COMMIT cpu-all vulkan"
if [[ -f "$ROOT/opt/aios/runtime/COMMIT" && $(cat "$ROOT/opt/aios/runtime/COMMIT") == "$STAMP" && -f "$ROOT/opt/aios/runtime/lib/libggml-vulkan.so" ]]; then
  exit 0
fi
# glslc compiles the compute shaders, SPIR-V headers and the loader development
# files let CMake find Vulkan. They stay in the cache and are purged from images.
chroot "$ROOT" apt-get "${APT[@]}" update
chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends glslc libvulkan-dev spirv-headers
[[ -f build/llama.tar.gz ]] || curl --fail --location --retry 3 "https://github.com/ggml-org/llama.cpp/archive/$LLAMA_COMMIT.tar.gz" -o build/llama.tar.gz
# An interrupted build of the same commit and options resumes where it stopped:
# the Vulkan backend alone takes most of an hour on a small build host.
if [[ $(cat "$ROOT/usr/src/llama-build/AIOS_STAMP" 2>/dev/null) != "$STAMP" ]]; then
  rm -rf "$ROOT/usr/src/llama.cpp-$LLAMA_COMMIT" "$ROOT/usr/src/llama-build"
  tar -xzf build/llama.tar.gz -C "$ROOT/usr/src"
  chroot "$ROOT" cmake -S "/usr/src/llama.cpp-$LLAMA_COMMIT" -B /usr/src/llama-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_VULKAN=ON \
    -DLLAMA_CURL=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=ON -DLLAMA_BUILD_SERVER=ON \
    -DCMAKE_INSTALL_PREFIX=/opt/aios/runtime -DCMAKE_INSTALL_RPATH=/opt/aios/runtime/lib
  echo "$STAMP" > "$ROOT/usr/src/llama-build/AIOS_STAMP"
fi
# Some generated shader sources need several GiB each to compile; when two at once
# exhaust the memory, finish the rest one at a time.
chroot "$ROOT" cmake --build /usr/src/llama-build --target llama-server ggml-vulkan -j2 ||
  chroot "$ROOT" cmake --build /usr/src/llama-build --target llama-server ggml-vulkan -j1
rm -rf "$ROOT/opt/aios/runtime"
mkdir -p "$ROOT/opt/aios/runtime/bin" "$ROOT/opt/aios/runtime/lib"
cp "$ROOT/usr/src/llama-build/bin/llama-server" "$ROOT/opt/aios/runtime/bin/"
cp -a "$ROOT"/usr/src/llama-build/bin/*.so* "$ROOT/opt/aios/runtime/lib/"
[[ -f "$ROOT/opt/aios/runtime/lib/libggml-vulkan.so" ]] || { echo 'Vulkan backend missing from the build'; exit 1; }
echo "$STAMP" > "$ROOT/opt/aios/runtime/COMMIT"
