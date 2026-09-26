# Verification

This page says what is actually checked, with what, and how to reproduce it. Everything here is run against the real appliance, not mocks.

## Automated tests

```bash
make test                      # lint (Ruff), types (mypy), backend tests, frontend tests and build
make test-image                # boots the raw image in QEMU
AIOS_BROWSER_TEST=1 AIOS_ADMIN_TEST=1 make test-image
make test-iso                  # boots the ISO, installs on an empty disk, boots the result
./scripts/test-installer-qemu.sh
(cd dist && sha256sum -c aios-installer-1.9.0-x86_64.iso.sha256)
```

The backend tests use temporary directories, a real SQLite database in WAL mode, the real GGUF parser, real Argon2id authentication and a controlled HTTP transport. Fixtures never execute code. Areas covered include:

- authentication, roles, CSRF, session revocation and the console password gate, including the shared backoff;
- repository discovery: publisher queries, exclusion rules, migration of the old fixed lists, pruning, progressive saving and tolerance for a single broken model;
- vision models: the projector found beside each model file (precision and per-model naming), downloaded and verified with it, kept out when broken without losing the model, fetched later for models installed without it, and passed to llama-server (`tests/test_vision.py`);
- what the catalogue may list: multimodal projectors as standalone models, split models, speculative-decoding drafts, importance matrices and adapters, refused from their GGUF structure at discovery, download, publication and start;
- the RAM estimate and the context fit, including the reduction of the default 101024-token window and the trained-window cap;
- the reasoning budget passed to llama.cpp;
- downloads: resume, retries, checksum mismatch, database contention;
- the installer wizard: per-field validation and re-prompting;
- the closed console and the Open WebUI defaults that keep the CPU free between answers;
- image generation: the diffusion catalogue with its components, models whose components are not published openly, the memory rating, the engine command line for a checkpoint and for a split model, the second port, installation and deletion of every file, and the refusal to treat a diffusion model as a chat model (`tests/test_imaging.py`);
- GPU acceleration: device parsing and classification, the discrete/integrated choice, memory counting, the NVIDIA module flavour for each card generation, and the automatic return to the CPU when a GPU start fails (`tests/test_accelerators.py`).

The image and ISO tests boot a real kernel and the real services in QEMU, with a temporary overlay; they never touch the distributed image or a physical disk. They check UEFI and BIOS boot, HTTPS, the portal and the API, the bootstrap flow, Open WebUI, and — unless `AIOS_MODEL_SMOKE=0` — download a ~1.2 MB GGUF and run a real chat.

## Verification on a lab appliance

The lab checks live in [integration-tests/lab](../integration-tests/lab/README.md) and run with `make lab-test ISO=...`: they install the ISO on a fresh virtual machine through the guided installer, then exercise the portal in a real browser, the API, the chat and the operations that change the system.


Every release is also installed from its own ISO onto a two-core, 3 GiB virtual machine and exercised end to end: guided installation, boot splash, console menu, administrator creation, repository sync, model download, publication, start, chat in Open WebUI, and the OpenAI-compatible API from outside with the inference key. The screenshots in [the demo](demo.md) come from that appliance.

GPU support is checked on that same lab appliance, which has no GPU, in two passes:

- **No GPU.** The Hardware page reports no usable GPU, the runtime starts llama.cpp with `--device none` and the model answers on the CPU as before.
- **Forced GPU path.** Mesa's software renderer (lavapipe) is made visible as a Vulkan device with `AIOS_GPU_ALLOW_SOFTWARE=1` and `GGML_VK_VISIBLE_DEVICES=0` on the runtime manager and the hardware profiler. The Hardware page then lists `Vulkan0`, the runtime starts the model on it, the Runtime page shows the device, and a chat completion through `/v1/chat/completions` returns a correct answer computed by the Vulkan backend (slowly, since the "GPU" is the same CPU). This exercises detection, device selection, the llama.cpp Vulkan backend, the offload and the portal end to end; it does not measure the speed of a real card.

Image input is checked there with a real vision model from its real repository: `unsloth/Qwen3-VL-2B-Instruct-GGUF` is synchronised, the catalogue lists its model files with `mmproj-F16.gguf` attached (and no projector as a model of its own), the install downloads and verifies both, llama-server logs that it loaded the multimodal projector, and a picture sent through `/v1/chat/completions` — half red, half blue — is answered correctly.

Image generation is checked on the same lab appliance, which has no GPU: the diffusion repository is enabled and synchronised against the real Hugging Face repositories (six files across three families, with FLUX.1 schnell's three component files resolved from two other repositories), the rating refuses what does not fit, Stable Diffusion 1.5 is installed and published, the portal reports it as an image model and the chat's model list stays empty, and **Generate a picture** produces a 384×384 picture in 8 steps in 274 s on two vCPUs. The same engine on four threads of the build host produces 512×512 in 20 steps in 218 s.

The image build finishes only when the final system loads the Vulkan backend and sees that software device, so every image that ships is one where GPU acceleration is in place.

Every shipped repository is synchronised against the real service before a release, not only against recorded responses: Hugging Face (the open listing and the three curated families), ModelScope, and the diffusion catalogue. The September 2026 run found 118, 702, 193, 537 and 6 files respectively, and ModelScope 65 files from its four publishers. Providers that need configuration — GitHub, and an Internal or HTTP manifest without a URL — must report NOT CONFIGURED with the reason.

## What the verification covers

- **The image that ships.** Every build records its package inventory in `build/*.lock` and its commit, kernel and component versions in the build info, so an image can always be traced back to what went into it. Two builds of the same sources are equivalent rather than byte-identical: UUIDs, timestamps and refreshed Ubuntu packages differ.
- **GPU acceleration** is exercised through the software Vulkan renderer described above, which proves device selection, layer offload and the CPU fallback on every build. Throughput on a given card depends on the CPU or GPU, the quantisation and the context, and is measured on the machine itself with the benchmark in the portal.
- **Boot and firmware.** The image boots under UEFI and legacy BIOS, with Secure Boot disabled in the firmware.
- **Storage.** Data is written to the disk as it is; use encrypted storage, or hypervisor encryption, where the machine can be physically reached.
- **Upgrades.** Parts of AIOS are replaced from signed archives ([backup and restore](backup-restore.md)); moving to a new image is done by installing it and restoring a backup.
- **Network placement.** The appliance is designed for a LAN with identified administrators.
