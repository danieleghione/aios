# Development and tests

Layout: `backend/aios` holds the FastAPI control plane, the schema, authentication, the hardware profiler, repository providers, the downloader, the runtime manager and the privileged broker; `frontend-admin` holds the React/TypeScript portal; `config`, `systemd`, `installer`, `scripts` and `branding` describe the appliance itself. `build` and `dist` are artefacts and stay out of Git, except `build/versions.env`.

```bash
./scripts/setup-host.sh
make build
make test
```

Unit and integration tests use temporary directories, a real SQLite database, the real GGUF parser, real authentication and a controlled HTTP transport in the offline tests. Fixtures never execute code. The image test boots a real kernel and the real services and, by default, reaches real HTTPS repositories and real GGUF files. `AIOS_MODEL_SMOKE=0 make test-image` checks boot and services without downloading a model; it does not replace the full inference test.

Running the backend for development:

```bash
AIOS_DATA="$PWD/build/dev-data" AIOS_ETC="$PWD/build/dev-etc" .venv/bin/python -m aios init
AIOS_DATA="$PWD/build/dev-data" AIOS_ETC="$PWD/build/dev-etc" .venv/bin/uvicorn aios.app:app --host 127.0.0.1 --port 8081
```

Signing in from a browser needs a local HTTPS reverse proxy: Secure cookies are not weakened in development. Start the download and runtime workers separately, with the same variables, when you work on the lifecycle. Do not start `platform` on a development machine: it is meant for the appliance and changes the network and OS services with root privileges.

Public APIs use Pydantic models, consistent authorisation and uniform errors. Do not add shell calls driven by input. A new provider must respect the metadata limits, validate and pin URLs and keep credentials separate. A new migration needs a schema increment and an upgrade/backup compatibility test.

GPU code can be tested without a GPU: `tests/test_accelerators.py` feeds recorded `llama-server --list-devices` and `vulkaninfo` output, and `AIOS_PCI_DEVICES` points `scripts/nvidia-driver.sh --decide` at a fake PCI tree. On an appliance without a GPU, setting `AIOS_GPU_ALLOW_SOFTWARE=1` and `GGML_VK_VISIBLE_DEVICES=0` on `aios-runtime-manager` makes the Mesa software renderer (lavapipe) stand in for a GPU, which exercises the whole path — detection, device arguments, offload and the portal — slowly.

CI runs Ruff, mypy, the backend tests, the frontend tests, the TypeScript typecheck and build, and dependency scans. The image job is manual because it needs root, a lot of storage and time. Console logs from the tests contain temporary secrets and are never uploaded as public artefacts.

The optional `scripts/refresh-image-app.sh` needs the `build/rootfs` cache, with its compiler, and every VM stopped: it builds the benchmark inside that Ubuntu cache, because the final raw image carries no compilers. It only refreshes a development image and invalidates checksums and build info; for anything you hand over, always use `make image` and run the tests again.
