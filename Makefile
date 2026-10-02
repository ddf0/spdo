.PHONY: install up down run migrate lint fmt test metrics metrics-before metrics-after docs-api docs-api-docx docs-gost uml

MD2GOST ?= tools/md2gost/md2gost/md2gost.js
PLANTUML ?= plantuml
GOST_SRC := $(filter-out docs/gost/src/_%,$(wildcard docs/gost/src/*.md))

install:
	uv sync
	@if [ -f tools/md2gost/md2gost/package.json ]; then cd tools/md2gost/md2gost && npm ci --silent; fi

up:
	docker compose up -d --build

down:
	docker compose down

migrate:
	uv run alembic upgrade head

run:
	uv run uvicorn spdo.main:app --reload

lint:
	uv run ruff check src
	uv run ruff format --check src

fmt:
	uv run ruff format src
	uv run ruff check --fix src

test:
	uv run pytest

metrics:
	@mkdir -p reports/$(or $(TAG),current)
	uv run radon cc src -s -a        > reports/$(or $(TAG),current)/cc.txt
	uv run radon mi src -s           > reports/$(or $(TAG),current)/mi.txt
	uv run radon raw src -s          > reports/$(or $(TAG),current)/raw.txt
	uv run radon hal src             > reports/$(or $(TAG),current)/hal.txt
	uv run radon cc src -j           > reports/$(or $(TAG),current)/cc.json
	@echo "Отчёты: reports/$(or $(TAG),current)/"

metrics-before:
	$(MAKE) metrics TAG=metrics-before

metrics-after:
	$(MAKE) metrics TAG=metrics-after

docs-api:
	uv run sphinx-build -b html -W --keep-going docs/api docs/api/_build/html

docs-api-docx:
	uv run python docs/api/gost_md.py docs/api/_build/gost
	@mkdir -p docs/api/_build/docx
	node $(MD2GOST) docs/api/_build/gost/api.md -p espd -o docs/api/_build/docx/spdo-api.docx

docs-gost:
	@mkdir -p docs/gost/build
	@for f in $(GOST_SRC); do \
	  p=$$(sed -n 's/^profile:[[:space:]]*\([a-z]*\).*/\1/p' $$f | head -1); \
	  out=docs/gost/build/$$(basename $$f .md).docx; \
	  echo "md2gost [$$p] $$f -> $$out"; \
	  node $(MD2GOST) $$f -p $$p -o $$out || exit 1; \
	done

uml:
	$(PLANTUML) -tpng -charset UTF-8 -o png docs/uml/[0-9]*.puml
