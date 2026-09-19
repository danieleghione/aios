# Models and runtimes

Discovery downloads metadata only. Every source/revision pair keeps its own UUID; a new revision never replaces an installation by itself. Weights are stored under their UUID, whatever they are called upstream.

Lifecycle: DISCOVERED → QUEUED → DOWNLOADING → VERIFYING → INSTALLED → PUBLISHED. FAILED records the error and the retries; PAUSED and CANCELLED are queue states. DISABLED marks an installation whose file is gone, for example after a restore without weights. Publishing and installing stay separate.

The downloader checks HTTPS, redirects, size, free space and the upstream checksum when there is one; it always computes a local SHA256. When upstream publishes no hash, the local checksum protects later integrity — it does not prove the source is authentic. The GGUF parser limits metadata to 16 MiB, strings to 1 MiB, and the number of arrays, tensors and offsets. Loading the model is the final compatibility check: a structurally valid GGUF can still be refused by the engine.

The RAM estimate covers weights, overhead, the KV cache and a 1.5 GiB reserve for the OS; it uses the GGUF metadata when available and a conservative guess otherwise. OPTIMAL, COMPATIBLE, LIMITED, NOT_RECOMMENDED and INCOMPATIBLE always come with reasons. No throughput is promised: CPU, architecture, quantisation, memory bandwidth and context all matter. With a discrete GPU the free video memory counts too, and the rating names the GPU. The runtime checks free memory again before loading. The image creates no swap; `vm.swappiness=1` limits the use of any swap an operator adds.

Profiles: AUTO (threads from the calibration), LOW_MEMORY (smaller context and batch), BALANCED, MAX_PERFORMANCE and CUSTOM. Parameters: acceleration (Automatic, GPU only, CPU only — see [GPU acceleration](gpu.md)), context, threads, batch, parallel requests, affinity, NUMA, mlock, embeddings, timeout, autostart and reasoning budget. Context is the server's total budget, shared between parallel slots as llama.cpp does it.

The default context is **101024 tokens**: not a promise, but the maximum asked for. At start-up it is reduced to the smaller of the window the model declares in its GGUF header and the window free RAM allows, halving until it fits; the Runtime page shows the context really in use. A context written explicitly in the configuration is honoured even beyond the model's declared window, and if it does not fit the start is refused with the amount of RAM it would need. For embeddings use a suitable model and the dedicated option; an embedding runtime is not a chat runtime.

`reasoning_budget` limits the thinking tokens of models that reason before answering (Qwen3.5/3.8, DeepSeek R1/V4, Magistral and similar). The default `-2` (automatic) gives reasoning half of the context a single request may use (context ÷ parallel requests); at that limit llama.cpp closes the reasoning and the model has to give its final answer. Without a limit, small models can keep reasoning until the context is full and stop without answering at all. `-1` removes the limit, `0` disables reasoning, a positive value is a fixed budget. Models that do not reason are unaffected.

A single language model is kept loaded — stop or unload frees the RAM before the next start — beside at most one image model, which uses a different engine ([image generation](image-generation.md)). Published but inactive models are listed by `/v1/models`, and requests get 409 until they are started. The gateway keeps SSE streaming, timeouts and concurrency limits. Open WebUI uses the local inference key automatically, stored at `/etc/aios/secrets/inference-key`.

An explicit HugeTLB request is refused by the backend when it is not supported: reserving pages in the kernel does not mean llama.cpp uses them. Transparent HugePages depend on the kernel policy. 1 GiB pages, AMX and advanced NUMA need suitable hardware and kernels, and are not switched on blindly. Upstream picks the best CPU backend among those compiled in.

The real inference test uses `ggml-org/models/tinyllamas/stories260K.gguf`, about 1.2 MB, downloaded only inside the test VM and never shipped in the image. It is a tiny model for checking execution and the API, not conversation quality. The offline test fixture is synthetic GGUF data and is never treated as a runnable LLM.

## What the catalogue lists

The catalogue only lists files this appliance can run on its own. Discovery therefore excludes, for every provider:

- **multimodal projectors** (`mmproj-*.gguf`, or the `clip` architecture) as models of their own: they hold the vision encoder, not the language weights, and `llama-server` cannot load one as a model. They are not discarded, though: see [Image input](#image-input) below;
- **models split into parts** (`*-00001-of-0000N.gguf`): a single part cannot be loaded without the others, and an installation downloads one file;
- **speculative-decoding drafts** (`mtp-`, `dflash-`, `dspark-` and the like) and other non-model GGUF files (importance matrices, LoRA adapters). They are recognised from the structure of the header, not from the name: a file is excluded when `general.type` is not `model`, when it declares `<arch>.target_layers` (the layers of another model it reads from), or when it holds far fewer tensors than its declared blocks;
- **architectures the bundled llama.cpp cannot load**, checked against the list recorded from its own sources at build time.

The same files are refused at download, publication and start, so a model installed before these checks existed cannot reach the chat and fail there with an obscure error.

## Image generation

Diffusion models are installed and started through the same pages, with their own engine, ports and memory rules: see [image generation](image-generation.md). They never appear in the chat's model selector.

## Image input

Vision models — Qwen-VL, Gemma 3, Mistral Small 3.x, SmolVLM and the like — read images only with their **multimodal projector**, a separate `mmproj-*.gguf` file published in the same repository. Without it the model still answers text, and every image sent to it is refused with *image input is not supported*.

AIOS therefore keeps the projector with the model:

- **Discovery** records, for every model file, the projector published beside it. When a repository offers several precisions, F16 is chosen (BF16, Q8_0 and F32 follow); when it offers one projector per model size, only the one named after that model is used.
- The **catalogue** marks those models *IMAGE INPUT*, and the details say how large the projector is. The memory estimate includes it, since llama.cpp reads it into memory whole.
- **Installation** downloads the model and its projector in one job, with the same resume, checksum and GGUF checks. A projector that fails its checks does not cost the model: the model is installed for text, and the download says why image input is missing.
- **Start** passes the projector to llama-server with `--mmproj`; on the CPU it also keeps the projector off any GPU (`--no-mmproj-offload`).
- A vision model installed **before** projectors were tracked shows *Add image input* under **Installed models** after its repository is synchronised again: it downloads just the projector. Restart the model afterwards.

The chat needs nothing else: Open WebUI sends images to any model, and the model now reads them.

