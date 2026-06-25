# Mirrors the CI test leg locally.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test         - fresh venv, install .[dev], run the suite (matches CI)
#   make test-all     - alias for test (Meninges has a single [dev] leg)
#   make clean        - remove any leftover venvs
#
# Each Python target creates a fresh isolated venv, runs tests, then removes it.
# Venvs are also gitignored as a belt-and-suspenders safety net.

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR    := SerenMeninges

VENV_BASE  := .venv-base

.PHONY: test

test:
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_BASE); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-base\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev]"; \
	.\.venv-base\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	exit $$status

test-all: test

clean:
	Remove-Item -Recurse -Force $(VENV_BASE)
