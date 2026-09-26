# Contributing to AIOS

Thank you for helping. AIOS is an appliance: every change ends up on someone's machine without a shell to fix it, so changes are judged by how they behave on a real installation.

## Before you start

- **Reports**: open an issue with the matching form. For hardware, the *Hardware report* form asks for exactly what we need (CPU, GPU, RAM, what the Hardware page shows).
- **Larger changes**: open an issue first and describe the problem, not only the solution.
- **Security problems**: do not open a public issue; see [SECURITY.md](SECURITY.md).

## Working on the code

The layout, the development loop and the build are described in [docs/development.md](docs/development.md) and [docs/build.md](docs/build.md). In short:

```bash
make build   # backend virtualenv and portal
make test    # ruff, mypy, pytest and the portal tests
make iso     # full image and installer ISO (needs root, about 45 minutes)
```

A pull request should:

1. keep `make test` green and add a test that pins the behaviour it changes;
2. say how it was verified on an installed appliance when it touches the installer, the runtime, the platform broker or the portal (a QEMU virtual machine is enough: see [docs/verification.md](docs/verification.md));
3. update the documentation in `docs/`, and add a line to [CHANGELOG.md](CHANGELOG.md) when users see the difference;
4. keep the interface in English and match the style of the surrounding code; comments explain *why*.

## What we are careful about

- Nothing leaves the machine unless an administrator configured it to (repositories, updates).
- The console stays closed; privileged work goes through the platform broker, never through the web process.
- Models are only offered when this appliance can really run them: the catalogue lists what starts here.

By contributing you agree that your contribution is released under the project's [licence](LICENSE).
