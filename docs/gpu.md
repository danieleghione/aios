# GPU acceleration

AIOS uses a GPU when the machine has one and the CPU when it does not. There is nothing to install or configure: the same image works on a laptop with integrated graphics, a server with several NVIDIA cards, an AMD workstation or a VM with a GPU passed through.

## How it works

llama.cpp is built with its **Vulkan** backend next to the CPU backends. Vulkan is the one GPU interface that every vendor supports on Linux, through drivers Ubuntu ships:

| GPU | Vulkan driver in the image | Kernel driver |
|---|---|---|
| NVIDIA GeForce, RTX, Quadro, Tesla — Maxwell (GTX 900) and newer | NVIDIA 580 (server branch) | NVIDIA modules, installed at boot for the cards present |
| NVIDIA Kepler (GTX 600/700) and older | Mesa NVK, where it supports the card | nouveau |
| AMD Radeon, discrete or integrated (Radeon HD 7000 and newer) | Mesa RADV | amdgpu |
| Intel Arc (Alchemist, Battlemage) and integrated Intel graphics (Skylake and newer) | Mesa ANV | i915 / xe |
| Older integrated Intel graphics (Ivy Bridge to Broadwell) | Mesa hasvk | i915 |

The image uses the Ubuntu 24.04 hardware enablement kernel, so recent GPUs — Intel Battlemage, Lunar Lake and Panther Lake graphics, AMD Radeon RX 9000 — are recognised as well.

NVIDIA ships two kernel module flavours and neither covers every card: the open modules are required from the RTX 50 series on and support Turing (RTX 20 / GTX 16) and newer, while Pascal and Maxwell cards need the proprietary modules. Both are carried in the image as packages. At boot `aios-nvidia-driver` looks at the NVIDIA cards on the PCI bus, installs the matching flavour offline for the running kernel and loads it; if one card is older than Turing, the proprietary modules are used because they drive both generations. Machines without an NVIDIA GPU skip this step entirely. Cards the current driver no longer supports get the open-source nouveau driver instead.

## Which device a model uses

Before every start the runtime manager asks llama.cpp which GPUs it can use, then:

- every **discrete GPU** is used when there is at least one; llama.cpp splits the model across them;
- otherwise the **integrated GPU** is used;
- otherwise the model runs on the **CPU**.

Software renderers (llvmpipe) and virtual display adapters are never treated as a GPU. Layers that do not fit in video memory stay in system RAM and run on the CPU, so a model larger than the card still starts.

The runtime configuration of each model has an **Acceleration** setting:

- **Automatic** (default): the choice above. If the model fails to start on the GPU — not enough video memory, a driver error — it is started again on the CPU, and the Runtime page says so, with the error.
- **GPU only**: the same choice, but a start without a usable GPU, or a failed GPU start, is reported as a failure instead of falling back.
- **CPU only**: never offload.

## What you see in the portal

- **Hardware → GPU acceleration** lists the devices found, whether each is dedicated or integrated, its memory and driver, and which ones models use. If the NVIDIA driver could not be loaded, the reason is shown there.
- **Runtime** shows where each running model is placed: *Running on the CPU*, or *Running on* the GPUs it uses, and the reason when a GPU start fell back to the CPU.
- **Image models** use the same devices through their own engine, and keep weights in RAM when the card has less free memory than the model ([image generation](image-generation.md)).
- **Catalogue** ratings count the free memory of discrete GPUs on top of system RAM, and name the GPU in the reasons. Integrated GPUs share system RAM, so they add none.

The context size is fitted to the memory really available — RAM plus video memory on a discrete GPU — as described in [Models and runtimes](model-management.md).

## Virtual machines

A GPU must be passed through to the VM (PCI passthrough, or an SR-IOV / GVT-g virtual function); an emulated display adapter is not a GPU. Proxmox steps are in [GPU passthrough](proxmox.md#gpu-passthrough). Other hypervisors work the same way as long as the guest sees the real PCI device.

## What the Vulkan path gives you

- One image that accelerates on every vendor. For NVIDIA cards the optional CUDA package below adds llama.cpp's CUDA backend, faster on many of them.
- Cards with Vulkan compute are used automatically; hardware without it (for example Radeon HD 6000 and older, Intel graphics before Ivy Bridge) runs its models on the CPU.
- The NVIDIA modules are loaded with Secure Boot disabled in the firmware, as the [installation requirements](install.md) ask.
- An NVIDIA card added after a system update that installed a new kernel needs network access once, because the bundled modules match the kernel the image shipped with.
- One model of each kind (language, image, speech, voice) is loaded at a time, each using the devices chosen above.

## CUDA for NVIDIA cards (optional)

The image stays the same for every vendor; NVIDIA cards can also run through llama.cpp's CUDA backend, installed as a signed component:

1. On the build machine, after `make image`: `make cuda-package`. It compiles the CUDA backend from the same llama.cpp commit as the image's runtime, in a copy of the build root, for every generation from Pascal to Hopper, and packs it with the CUDA runtime and cuBLAS libraries into `dist/aios-cuda-<version>.tar.gz`.
2. Sign it with the key the appliance trusts: `scripts/sign-release.py cuda <version> dist/aios-cuda-<version>.tar.gz --private-key <key.pem> --output cuda.json`.
3. Install it under **System → Updates → Application updates**, like any component release.

llama.cpp then loads the CUDA backend beside Vulkan (`GGML_BACKEND_PATH`), and a card seen by both is used once, through CUDA; other GPUs keep Vulkan. The NVIDIA driver the appliance installs at boot provides the rest. **Hardware** shows the installed package; *Remove* takes it out and the cards go back to Vulkan. The language and voice engines use it; the image and speech engines keep Vulkan.
