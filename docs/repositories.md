# Repositories

A repository that has not been configured yet — the GitHub provider without repositories, an Internal or HTTP manifest without a URL — reports **NOT CONFIGURED** with the reason, not ERROR: nothing is broken, it is waiting for a decision. Providers come preconfigured but disabled. Enabling one is an administrative decision; no release is ever installed automatically. The Internal provider has no initial URL because it depends on your own LAN: set the address of your HTTPS server. No provider answer is ever simulated.

## Hugging Face

Hugging Face uses the official Hub APIs (`/api/models`, metadata with `blobs=true`) and resolve URLs pinned to a commit. Without a token, public repositories are available; gated ones need a token and the licence accepted on the provider side. `models` restricts discovery to explicit IDs; without it, up to 30 recent GGUF models are read. `author` filters an organisation. This is not an unlimited crawl of the whole Hub.

**Discovering the newest releases**: `publishers` (up to 12 organisations) together with `search` (one or more words, up to 8) queries the Hub for every pair, ordered by creation date, merges the results and keeps the `limit` most recent distinct models this appliance can run (default 30, maximum 100; copies of the same model from different publishers count once and are all kept, while a model with nothing runnable takes no place). `exclude` replaces the default list of words rejected in a name (copies with the safety alignment removed, such as `abliterated`, `uncensored` or `dolphin`, plus embeddings, rerankers, OCR, speech synthesis and recognition, and base models). A fixed `models` list can never see a release published after it was written, and a plain name search across the Hub returns mostly anonymous re-uploads; known publishers plus a family keyword stay current at every sync.

Preconfigured repositories, all **disabled** after installation and enabled one by one:

| Name | publishers | search |
|---|---|---|
| Qwen (GGUF) | Qwen, ggml-org, unsloth, lmstudio-community | Qwen |
| DeepSeek (GGUF) | ggml-org, unsloth, lmstudio-community (deepseek-ai publishes no GGUF) | DeepSeek |
| Mistral (GGUF) | mistralai, ggml-org, unsloth, lmstudio-community | Mistral, Ministral, Devstral, Magistral |

Installations that still carry the original fixed lists, unmodified, switch to these queries at start-up; a list an administrator edited is left alone.

Every sync drops catalogue entries the repository no longer offers, except those installed or already downloaded. The catalogue shows only enabled repositories (plus installed models), ordered by release date, newest first.

## What is filtered out

Recent repositories publish, next to the model, files that cannot answer on their own: speculative-decoding drafts (`mtp-`, `dflash-`, `dspark-`), importance matrices (`imatrix`) and LoRA adapters. Their names are not enough to tell them apart, because they often declare the same architecture as the model; the structure read from the GGUF header decides. A file is excluded when `general.type` is not `model`, when it declares `<arch>.target_layers` (the layers of another model it reads from, as DFlash/EAGLE drafters do) or when it holds fewer than two tensors for each declared block (an MTP head carries only the last one). Sync reads one header per file layout — quantisations of the same file share a structure — and reuses what it read in later syncs. Installation checks the complete header again, including every block and the token embeddings, and refuses these files with a plain message; publishing, starting and chatting refuse in the same way for files installed by earlier versions, which should be deleted.

Models split into parts and architectures the bundled llama.cpp cannot load are excluded as well. Multimodal projectors (`mmproj-*.gguf` or the `clip` architecture) are not listed on their own: each is attached to the model files it belongs to and downloaded with them, so vision models accept images ([details](model-management.md#image-input)).

## ModelScope

ModelScope uses the repository API `/api/v1/models/{organization}/{model}/repo/files` and downloads through `/repo?Revision=...&FilePath=...`. `revision` defaults to `master`. Mutable revisions are recorded as received; prefer immutable revisions in managed repositories.

**Discovering the newest releases.** ModelScope has no per-author listing endpoint, but its search (`PUT /api/v1/dolphin/models`) matches the publisher's name as well as the model's. Each configured publisher is therefore queried for `"<publisher> <term>"` in two orders — newest first, which finds this week's releases, and relevance, which finds established repositories that a date sort buries — and only repositories that publisher actually owns, with GGUF in their name, are kept. The limit is then spent one publisher at a time, so no single prolific publisher takes every place.

Shipped configuration (disabled until enabled, like the others): publishers `Qwen`, `unsloth`, `lmstudio-community`, `ggml-org`, search `GGUF`, limit 20. An explicit `models` list still takes precedence and disables discovery, and `license` is used when the service does not expose one.

## GitHub

GitHub uses `/repos/{owner}/{repo}/releases`, up to 20 releases per repository. `models` holds `owner/repo` entries. GGUF assets may carry a SHA256 `digest`; without one, the local checksum is still computed after the download. Without a token the public rate limits apply.

Repository states: ONLINE, ERROR, AUTH REQUIRED, RATE LIMITED, SYNCING or DISABLED; every sync keeps its timestamp, duration and count.

## Generic HTTP and Internal

Both use this JSON manifest, served over HTTPS:

```json
{
  "schema_version": 1,
  "models": [{
    "model_id": "organization/example",
    "filename": "example-Q4_K_M.gguf",
    "url": "https://registry.example.org/models/example-Q4_K_M.gguf",
    "size": 123456789,
    "sha256": "64-hex-characters-of-the-real-file",
    "revision": "immutable-commit-or-release",
    "license": "MIT",
    "author": "organization",
    "architecture": "llama",
    "parameter_count": 1000000000,
    "context": 4096
  }]
}
```

The example documents the format: replace the values and the checksum with those of your own artefact. The parser refuses hashes that are not 64 hexadecimal characters, files that are not GGUF, names containing traversal and non-positive sizes. The JSON document is limited to 8 MiB and 10000 records.

Provider configuration in JSON:

```json
{"models":["organization/model"],"allow_private":false,"filters":{"license":"MIT","max_size":8589934592,"keyword":"Q4"}}
```

Available filters: author, architecture, quantization, license, keyword, max_size, min_parameters, max_parameters. Missing data is reported as unknown, never invented. The catalogue accepts further filters through the API, and saved searches (watchlists) through the portal. A watchlist stores filters over synchronised metadata: add the repository to the provider configuration as well, so its metadata is fetched.

For a LAN registry, enable `allow_private` explicitly; loopback, link-local and metadata service addresses stay forbidden. The connection uses the validated IP with the original Host/SNI to prevent DNS rebinding. HTTPS with certificate verification is mandatory; the proxy configured under System is explicit and never inherited from a development environment. Tokens are sent only to the provider host and dropped on redirects to other hosts. API redirects are revalidated, up to five, including upstream repository renames.

Integration sources: [Hugging Face Hub API](https://huggingface.co/docs/hub/api), [ModelScope Hub](https://github.com/modelscope/modelscope/tree/master/modelscope/hub), [GitHub REST releases](https://docs.github.com/en/rest/releases/releases).
