# Third-party components

AIOS does not change the licence of anything it bundles. Ubuntu packages keep their copyright and licences in `/usr/share/doc`. Distributing a complete image means honouring those licences, including the source-availability obligations of GPL components.

- **Ubuntu, Linux, systemd, NGINX and the userland utilities**: versions recorded in the build info, copyright inside the packages.
- **llama.cpp**: MIT licence; the source is the commit pinned in `build/versions.env`, from <https://github.com/ggml-org/llama.cpp>.
- **Mesa Vulkan drivers and the Vulkan loader**: MIT and related permissive licences, from the Ubuntu archive.
- **NVIDIA driver user-space libraries and kernel modules** (580 server branch): from the Ubuntu archive, under the NVIDIA software licence for the proprietary components and MIT/GPL-2.0 for the open kernel modules. They are redistributed unmodified, as Ubuntu distributes them and as the NVIDIA Software License allows Linux distributions to do; the proprietary modules are linked against the kernel on the machine that uses them.
- **stable-diffusion.cpp**: MIT licence; the source is the commit pinned in `build/versions.env`, from <https://github.com/leejet/stable-diffusion.cpp>, together with the ggml fork it pins.
- **Open WebUI**: upstream licence of release 0.11.3, including its branding requirements. The original branding and interface are kept; AIOS applies colours only, through the stylesheet Open WebUI loads from `/static/custom.css`. The downstream `+aios.1` wheel changes dependency constraints only, with recorded provenance.
- **React, Vite, TypeScript and the frontend dependencies**: licences in their npm distributions, versions in the lockfile.
- **Fonts and artwork**: the AIOS logo and the images derived from it are part of this repository and covered by its licence.
- **Diffusion model licences differ from language models**: Stable Diffusion 1.5 is CreativeML OpenRAIL-M, SDXL Turbo is under Stability AI's non-commercial licence, FLUX.1 schnell is Apache-2.0. The catalogue shows each licence and asks you to accept it before the download.
- **No model weights are included.** Whoever installs a model must read its licence. The tiny test model comes from the public `ggml-org/models` repository; read its model card and licence before redistributing it.

The MIT licence of the AIOS code does not grant rights over documents or components owned by third parties.
