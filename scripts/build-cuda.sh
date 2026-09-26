#!/usr/bin/env bash
# The optional CUDA package: llama.cpp's CUDA backend, from the same pinned
# commit and options as the runtime in the image, with the CUDA libraries it
# links against. It is built in a copy of the image's build root, so the CUDA
# toolkit never reaches the image, and it is installed on an appliance as the
# signed "cuda" component:
#
#   scripts/build-cuda.sh
#   scripts/sign-release.py cuda <version> dist/aios-cuda-<version>.tar.gz --private-key <key.pem> --output cuda.json
#
# The NVIDIA driver the image installs at boot provides libcuda; this package
# brings the rest.
set -Eeuo pipefail
if [[ $EUID != 0 ]]; then exec sudo "$0" "$@"; fi
cd "$(dirname "$0")/.."
source build/versions.env
BASE="$PWD/build/rootfs"
ROOT="$PWD/build/cuda-root"
APT=(-o Acquire::ForceIPv4=true -o Acquire::Retries=5)
# Pascal to Hopper: every generation the Ubuntu toolkit compiles for.
ARCHITECTURES=${CUDA_ARCHITECTURES:-61;70;75;80;86;89;90}
VERSION="${AIOS_VERSION}-cuda"
[[ -x "$BASE/opt/aios/runtime/bin/llama-server" ]] || { echo 'Build the image first (make image): the package must match its runtime'; exit 1; }
[[ -f build/llama.tar.gz ]] || { echo 'build/llama.tar.gz is missing: run make image first'; exit 1; }

mounted=()
cleanup() { for m in "${mounted[@]}"; do umount -l "$m" 2>/dev/null || true; done; }
trap cleanup EXIT
mkdir -p "$ROOT"
rsync -aHAX --delete --exclude=/proc --exclude=/sys --exclude=/dev --exclude=/usr/src --exclude=/opt/aios/webui "$BASE/" "$ROOT/"
mkdir -p "$ROOT/proc" "$ROOT/sys" "$ROOT/dev" "$ROOT/usr/src"
for m in proc sys dev; do mount --bind "/$m" "$ROOT/$m"; mounted+=("$ROOT/$m"); done
cp /etc/resolv.conf "$ROOT/etc/resolv.conf" 2>/dev/null || true
chroot "$ROOT" apt-get "${APT[@]}" update
# The toolkit's nvcc supports GCC up to 12 as the host compiler.
chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends \
  nvidia-cuda-toolkit gcc-12 g++-12 cmake ninja-build
tar -xzf build/llama.tar.gz -C "$ROOT/usr/src"
chroot "$ROOT" cmake -S "/usr/src/llama.cpp-$LLAMA_COMMIT" -B /usr/src/cuda-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$ARCHITECTURES" \
  -DCMAKE_CUDA_HOST_COMPILER=g++-12 -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF
chroot "$ROOT" cmake --build /usr/src/cuda-build --target ggml-cuda -j"$(nproc)"

stage=$(mktemp -d)
mkdir -p "$stage/lib"
cp "$ROOT/usr/src/cuda-build/bin/libggml-cuda.so" "$stage/lib/"
# The CUDA runtime and cuBLAS the backend links against, resolved inside the root.
for library in $(chroot "$ROOT" ldd /usr/src/cuda-build/bin/libggml-cuda.so | awk '/libcudart|libcublas/ {print $3}'); do
  cp -L "$ROOT$library" "$stage/lib/"
done
ls "$stage/lib" | grep -q libcudart || { echo 'The CUDA runtime library was not found'; exit 1; }
echo "$LLAMA_COMMIT cuda $ARCHITECTURES" > "$stage/COMMIT"
mkdir -p dist
tar -C "$stage" -czf "dist/aios-cuda-$VERSION.tar.gz" .
rm -rf "$stage"
sha256sum "dist/aios-cuda-$VERSION.tar.gz" | tee "dist/aios-cuda-$VERSION.tar.gz.sha256"
echo "Built dist/aios-cuda-$VERSION.tar.gz"
