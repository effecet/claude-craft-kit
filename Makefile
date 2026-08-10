.PHONY: help install lint syntax scan clean

CLAUDE_DIR ?= $(HOME)/.claude

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:        ## Copy hooks/ + rules/ + commands/ + CLAUDE.example.md into CLAUDE_DIR (default ~/.claude)
	@echo "Installing into $(CLAUDE_DIR) ..."
	@mkdir -p "$(CLAUDE_DIR)/hooks" "$(CLAUDE_DIR)/rules" "$(CLAUDE_DIR)/commands"
	@cp -r hooks/. "$(CLAUDE_DIR)/hooks/"
	@cp -r rules/. "$(CLAUDE_DIR)/rules/"
	@cp -r commands/. "$(CLAUDE_DIR)/commands/"
	@cp CLAUDE.example.md "$(CLAUDE_DIR)/CLAUDE.example.md"
	@echo "Done. Next steps:"
	@echo "  1. cp config.example.sh \"$(CLAUDE_DIR)/config.local.sh\" and source it"
	@echo "  2. merge settings.example.json's \"hooks\" block into \"$(CLAUDE_DIR)/settings.json\""
	@echo "  3. review CLAUDE.example.md, then save as \"$(CLAUDE_DIR)/CLAUDE.md\" if you want it"

syntax:         ## Syntax-check every hook (python + bash), no bytecode written
	@fail=0; \
	for f in $$(find hooks -name '*.py'); do \
	  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$$f" \
	    && echo "  ok  $$f" || { echo "  FAIL $$f"; fail=1; }; \
	done; \
	for f in $$(find hooks -name '*.sh'); do \
	  bash -n "$$f" && echo "  ok  $$f" || { echo "  FAIL $$f"; fail=1; }; \
	done; \
	[ $$fail -eq 0 ] && echo "All hooks OK" || exit 1

lint:           ## Lint hooks (ruff for python, shellcheck for bash) if installed
	@command -v ruff >/dev/null 2>&1 && ruff check hooks || echo "ruff not installed — skipping python lint"
	@command -v shellcheck >/dev/null 2>&1 && shellcheck hooks/*.sh || echo "shellcheck not installed — skipping shell lint"

scan:           ## Run gitleaks secret scan (if installed)
	@command -v gitleaks >/dev/null 2>&1 && gitleaks detect --no-banner --config .gitleaks.toml \
		|| echo "gitleaks not installed — see https://github.com/gitleaks/gitleaks"

clean:          ## Remove Python bytecode caches
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null; \
	find . -name '*.pyc' -delete 2>/dev/null; echo "cleaned"
