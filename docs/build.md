# Building from source

Supported host: Ubuntu 24.04 LTS x86-64 with sudo, at least 6 GiB of RAM and 30 GiB free (40 GiB is more comfortable for parallel clean builds and test images). Stop any virtual machine before a first build: a few of llama.cpp's Vulkan shader sources need several GiB each to compile. The first build downloads OS packages, the llama.cpp sources, Python and Node dependencies and the NLTK resources. Chatting after the models are installed needs no cloud.

Install the tools:

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends debootstrap qemu-system-x86 ovmf gdisk \
  dosfstools e2fsprogs parted rsync zstd build-essential cmake ninja-build \
  libcurl4-openssl-dev python3-venv python3-dev mtools uuid-runtime curl git \
  squashfs-tools xorriso
```

Install Node.js 22.22.1 or a later 22 release from the official Node.js distribution, with npm 11.6 or later. Check `node --version` and `npm --version`. Frontend package versions live in the lockfile and `npm ci` restores them. The build system does not need Docker.

```bash
make build        # backend and frontend
make test         # lint, types, backend and frontend tests
make image        # dist/aios-x86_64.img
make iso          # dist/aios-installer-x86_64.iso (builds the image first)
make test-image   # boots the raw image in QEMU
make test-iso     # boots the ISO in QEMU and installs it on an empty disk
```

`make image` installs the application dependencies, creates or reuses `build/rootfs`, compiles both inference engines — llama.cpp (`scripts/build-runtime.sh`) and stable-diffusion.cpp (`scripts/build-imaging.sh`) — with every x86 CPU variant and the Vulkan backend ( the first time this takes one to two hours on a small host, an interrupted compile resumes where it stopped and falls back to one job when memory runs out), installs the Ubuntu hardware enablement kernel, the Mesa and NVIDIA Vulkan drivers and the NVIDIA kernel module packages for that kernel, installs Open WebUI, generates the initramfs, partitions, bootloaders, splash theme and checksums. It uses loop devices and mounts under `/mnt/aios-build.*`; it never partitions a physical disk. The destructive installer is a separate program and is never run by the build.

Output:

- `dist/aios-x86_64.img` — sparse raw disk of 13 GiB logical size;
- `dist/aios-installer-x86_64.iso` — bootable installer (BIOS and UEFI), about 3.2 GB;
- `dist/*.sha256` — checksums;
- `dist/aios-x86_64-build-info.json` and `dist/aios-installer-build-info.json` — the versions and package inventories actually used;
- `dist/qemu-test-report.json` — the result of the VM verification.

The build is automatable and versioned, but not bit-for-bit deterministic: UUIDs, timestamps and refreshed Ubuntu security packages vary. The `build/*.lock` inventories and the build info record exactly what was produced. The llama.cpp commit is pinned in `build/versions.env`; Open WebUI and the downstream patches are versioned. Do not promise binary reproducibility without a snapshot mirror of the OS packages.

For a completely clean OS build, stop every build and VM first and check that `findmnt -R build/rootfs` lists no active mounts; then remove **only** `build/rootfs` and `build/rootfs-ready` and run `make image` again. `make clean` removes the image and frontend artefacts while keeping the OS cache. Never remove a rootfs recursively while `/dev`, `/proc` or `/sys` are still mounted inside it.

QEMU uses KVM when available and TCG otherwise. Defaults: 3 GiB of RAM, 2 vCPUs, HTTPS on the host at `127.0.0.1:18443`. Overrides: `AIOS_TEST_RAM`, `AIOS_TEST_PORT`, and `AIOS_MODEL_SMOKE=0` for a boot test without external network access. The test uses a qcow2 overlay and never initialises or modifies the distributed image. Console logs, which contain temporary secrets, are kept protected in `build/qemu-test/`.
