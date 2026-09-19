# Image generation

From version 1.5 AIOS runs diffusion models as well as language models. A picture is generated on the appliance itself, by a second engine — [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp), the same ggml foundation as llama.cpp — with the same rules as the rest of the system: devices chosen automatically, memory estimated before the start, nothing sent anywhere.

## What you can run

| Family | Files | Typical size | Notes |
|---|---|---|---|
| Stable Diffusion 1.5 | one checkpoint | 2 GiB | the lightest, and the only one that is reasonable without a GPU |
| SDXL Turbo | one checkpoint | 6.6 GiB | four steps per picture; non-commercial licence |
| FLUX.1 schnell | transformer + two text encoders + VAE | 7–12 GiB | best quality of the three; needs a GPU to be pleasant |

The catalogue marks them **IMAGE MODEL**. The Repositories page has an *Image models (diffusion)* entry, disabled like every other repository until you enable it.

## How it works

- **Discovery** lists the files each family needs. A diffusion model is often four files published in three different repositories; AIOS resolves them, and a model whose components are not published openly is not offered at all rather than failing at start.
- **Installation** downloads the model and every component in one job, with the same resume, checksum and free-space checks as a language model. Everything is stored under the installation UUID, so deleting a model deletes all of it.
- **Start** launches `sd-server` on loopback port 8091 — the language engine keeps port 8090 — with the devices AIOS chose, the family's defaults (size, steps, guidance, sampler) and, when the GPU has less free memory than the model, weights kept in RAM and streamed to the card.
- **One model of each kind.** A language model and an image model can be loaded at the same time; a second model of the same kind must wait for the first to stop.

## Generating a picture

**From the chat.** Open WebUI's image button is configured to ask this appliance: the request goes to the gateway with the local inference key, and the published diffusion model answers. If none is published, the chat shows a clear message instead of a mysterious failure.

**From the portal.** Under **Installed models**, an image model has a *Generate a picture* button: prompt, size and steps, and the result appears in the page with the time it took. It is the quickest way to check the engine after installing a model.

**From your own tools.** The OpenAI-compatible endpoint takes the usual request:

```bash
curl -k https://IP/v1/images/generations \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"prompt":"a lighthouse at sunset, photorealistic","size":"512x512"}' \
  | python3 -c "import json,sys,base64; open('out.png','wb').write(base64.b64decode(json.load(sys.stdin)['data'][0]['b64_json']))"
```

The answer is OpenAI-shaped: `data[0].b64_json` holds a PNG. Steps and other engine controls can be attached to the prompt in the engine's own extension block, for example `… <sd_cpp_extra_args>{"sample_params":{"sample_steps":30}}</sd_cpp_extra_args>`.

## How long it takes

Denoising is compute-bound, unlike text generation, so a GPU changes everything:

| Hardware | Stable Diffusion 1.5, 512×512, 20 steps |
|---|---|
| Discrete GPU (Vulkan) | seconds |
| Integrated GPU | roughly one to a few minutes |
| CPU only | **218 s measured** on four threads of an Intel Core i5-7600T |

On the two-vCPU lab appliance, a smaller picture — 384×384 in 8 steps — took 274 s.

The gateway therefore allows a request to run for up to 30 minutes, where a chat request gets the runtime timeout. On a machine without a GPU, use SD 1.5, small sizes and few steps, and expect to wait.

## Memory

Every file of a diffusion model is held in memory for the whole pass — there is no memory-mapped eviction as for a language model — plus about 1.5 GiB of working buffers. The catalogue rating for an image model counts exactly that, and says so in its reasons.

## Limits

- No video models, no image editing from the portal, no LoRA management yet: the engine supports them, AIOS does not expose them.
- Models whose components are published only in gated repositories (FLUX.1 dev, SD 3.5) need a repository token; they are not offered by default.
- The chat sends its own defaults (512×512, 20 steps) unless an administrator changes them in Open WebUI.
- Image models never appear in the chat's model selector: they answer `/v1/images/generations`, not `/v1/chat/completions`.
