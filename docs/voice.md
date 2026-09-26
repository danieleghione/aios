# Speech: listening and speaking

From version 1.8 AIOS also turns speech into text. Transcription runs on the appliance itself, by a third engine — [whisper.cpp](https://github.com/ggml-org/whisper.cpp), the same ggml foundation as the other two — with the same rules as the rest of the system: devices chosen automatically, memory estimated before the start, nothing sent anywhere.

## What you can run

The **Speech models (whisper)** repository offers the whisper builds published by the project, from the smallest to the largest:

| Model | Size | Good for |
|---|---|---|
| tiny, tiny.en | 32–75 MiB | the fastest, for short commands and clean audio |
| base, base.en | 57–141 MiB | everyday dictation on a machine without a GPU |
| small, small.en | 181–466 MiB | meetings and noisier recordings |
| medium, medium.en | 514 MiB–1.5 GiB | accents and overlapping voices |
| large-v3, large-v3 turbo | 547 MiB–2.9 GiB | the most accurate; the turbo builds are several times faster |

The `.en` models transcribe English only and are quicker at it; the others detect the language themselves. Quantised builds (`Q5_0`, `Q8_0`) carry the same weights in less memory. The catalogue marks them all **SPEECH MODEL** and rates them for this machine like any other model.

## How it works

- **One file per model**, downloaded and checksummed like any other, and kept under the installation identifier.
- **Start** launches `whisper-server` on loopback port 8092 — the language engine keeps 8090 and the image engine 8091 — with the devices AIOS chose.
- **One model of each kind.** A language model, an image model and a speech model can be loaded at the same time; a second model of the same kind waits for the first to stop.
- **Any recording format.** Browsers record WebM or MP4; the engine converts with the ffmpeg in the image, so WAV, MP3, M4A, OGG and WebM all work.

## Transcribing

**From the chat.** Open WebUI's microphone button records and sends the audio to this appliance, which answers with the text in the message box. The published speech model is the one that answers.

**From the portal.** Under **Installed models**, a speech model has a *Transcribe a file* button: choose a recording and the text appears with the time it took. It is the quickest way to check the engine after installing a model.

**From your own tools.** The OpenAI-compatible endpoint takes the usual request:

```bash
curl -k https://IP/v1/audio/transcriptions \
  -H "Authorization: Bearer <key>" \
  -F file=@meeting.m4a -F language=en
```

The answer is OpenAI-shaped: `{"text": "..."}`. A recording can be up to 200 MiB, about two hours of compressed audio. `language` is optional, and `prompt` gives the model context — names, jargon — to spell correctly.

## How long it takes

Transcription is several times faster than real time even without a GPU:

| Hardware | Audio of 11 seconds, Whisper tiny.en |
|---|---|
| CPU, four threads | **0.6 s measured** |
| GPU (Vulkan) | a fraction of that |

A larger model costs proportionally more: as a rule of thumb, *base* is about twice *tiny*, *small* about five times, *large-v3 turbo* about ten. The gateway allows a transcription to run up to 30 minutes, which covers a long recording on a small machine.

## Memory

A speech model is held whole in memory while it is loaded, plus about 0.5 GiB for the engine. The catalogue rating counts exactly that and says so in its reasons.

## Text to speech

From version 1.11 the appliance also speaks. A fourth engine reads text aloud with llama.cpp's own speech generation: the chat's *Read aloud* button, a *Speak a text* button in the portal and an OpenAI-compatible `/v1/audio/speech`. Like everything else it runs on the appliance, and the text is never sent anywhere.

**The models.** The **Voice models (text to speech)** repository offers Qwen3-TTS 1.7B (Apache 2.0), which speaks English, Italian, German, French, Spanish, Portuguese, Chinese, Japanese, Korean and Russian. Each quantisation comes with its codec, the file that turns the model's output into sound; both are downloaded, checked and installed together. The catalogue marks them **VOICE MODEL**.

**How it works.** *Start* launches a small service on loopback port 8094, next to the other engines. It uses almost no memory while it waits: for each text it splits the words into sentences, has `llama-tts` speak each one, and joins them with a short pause. One text is spoken at a time.

**The voice.** The model imitates the voice of a short recording; without one it would invent a new voice for every sentence. Every text is read with one of six voices, the same from the first sentence to the last. They are samples the model generated itself, not a real person's voice, and they keep the names OpenAI clients use:

| Voice | Pitch |
|---|---|
| `alloy` (default) | medium |
| `echo` | low |
| `fable` | medium-low |
| `onyx` | deep |
| `nova` | high |
| `shimmer` | bright |

In the portal, *Speak a text* has a *Voice* menu. In the chat, each user chooses the voice under **Settings → Audio**; Open WebUI reads the list from the appliance. Through the API, `voice` names one of them.

**The language** is the one the request names (`language`, or a language code as `voice`, such as `it`); otherwise it is detected from the text. The chat sends an answer one sentence at a time, so a short sentence that gives nothing away ("OK.", "42.") keeps the language of the sentence before it.

**From the chat.** When a voice model is published, Open WebUI's *Read aloud* button speaks through it; unpublishing it gives the button back to the browser's own voice. A text-to-speech engine an administrator chose in Open WebUI is left as it is.

**From your own tools:**

```bash
curl -k https://IP/v1/audio/speech \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"input": "Good morning, everyone.", "voice": "nova", "language": "en", "response_format": "mp3"}' -o hello.mp3
```

`voice` is one of the six above (a language code such as `it` chooses the language and keeps the default voice). `response_format` is `mp3` (the default), `wav`, `opus`, `aac`, `flac` or `pcm`; a text can be up to 4096 characters.

| Hardware | A sentence of 4 seconds, Qwen3-TTS 1.7B Q4_K_M |
|---|---|
| CPU, two threads | **about 16 s measured** |
| Virtual machine, two vCPU, 8 GiB | about 55 s measured |
| GPU (Vulkan) | a fraction of that |

**Memory.** While it speaks, the model needs its weights, a second copy of about two thirds of them that llama.cpp prepares for the CPU, the codec and about 1.3 GiB of working buffers: about 3.3 GiB for Qwen3-TTS 1.7B at Q4_K_M. The catalogue rating counts exactly that.
