.PHONY: help build dry-run release init-submodules

CI_PREFIX  := sw-integration
GIT_TAG    := $(shell git describe --tags --exact-match --match "$(CI_PREFIX)-v*" 2>/dev/null)

help:
	@echo "sw_integration targets:"
	@echo "  make init-submodules  — Clone/update fcs_model and ofp submodules"
	@echo "  make build            — Build without Docker (toolchain must be installed)"
	@echo "  make dry-run          — Show all build commands without executing"
	@echo "  make release          — Full release build + manifest seal (tag required)"

init-submodules:
	git submodule update --init --recursive

build:
	python3 build.py --skip-docker

dry-run:
	python3 build.py --dry-run --skip-docker

release:
	@if [ -z "$(GIT_TAG)" ]; then \
		echo "ERROR: HEAD is not on an exact $(CI_PREFIX)-vX.Y.Z tag."; \
		exit 1; \
	fi
	python3 build.py
