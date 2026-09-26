SHELL := /bin/bash
.PHONY: build image test test-image clean lint
build:
	./scripts/build.sh
image: build
	./scripts/build-image.sh
test:
	./scripts/test.sh
test-image:
	./scripts/test-image-qemu.sh
lint:
	.venv/bin/ruff check backend tests
	.venv/bin/mypy --follow-imports=silent --ignore-missing-imports backend/aios
	npm --prefix frontend-admin run typecheck
clean:
	rm -rf frontend-admin/dist dist/*.img dist/*.sha256 dist/*build-info.json

.PHONY: iso test-iso
iso: image
	./scripts/build-iso.sh
test-iso:
	./scripts/test-iso-qemu.sh

.PHONY: cuda-package
# The optional CUDA component for NVIDIA cards (see docs/gpu.md).
cuda-package:
	./scripts/build-cuda.sh

.PHONY: lab-test
# Install an ISO on a fresh lab VM and run every check (integration-tests/lab).
lab-test:
	integration-tests/lab/run.sh $(ISO)
