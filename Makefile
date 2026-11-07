.PHONY: help install migrate migrations test lint format shell run celery prod-build prod-build-docker

PYTHON = python
MANAGE = $(PYTHON) manage.py
SETTINGS_DEV = config.settings.dev
SETTINGS_TEST = config.settings.test

help:
	@echo "Cansee — Development Commands"
	@echo ""
	@echo "  make install       Install dev dependencies"
	@echo "  make migrate       Run pending migrations"
	@echo "  make migrations    Create new migrations"
	@echo "  make test          Run test suite"
	@echo "  make lint          Run ruff linter"
	@echo "  make format        Format code with black + isort"
	@echo "  make shell         Open Django shell"
	@echo "  make run           Start dev server"
	@echo "  make celery        Start Celery worker"
	@echo "  make check-all     Run lint + type check + tests"
	@echo "  make prod-build    Ruff, the same gate as CI lint"
	@echo "  make prod-build-docker  Build prod Docker images locally"

install:
	pip install -r requirements/dev.txt

migrate:
	DJANGO_SETTINGS_MODULE=$(SETTINGS_DEV) $(MANAGE) migrate

migrations:
	DJANGO_SETTINGS_MODULE=$(SETTINGS_DEV) $(MANAGE) makemigrations

test:
	pytest --cov=apps --cov-report=term-missing -v

lint:
	ruff check .

format:
	black .
	isort .

shell:
	DJANGO_SETTINGS_MODULE=$(SETTINGS_DEV) $(MANAGE) shell

run:
	DJANGO_SETTINGS_MODULE=$(SETTINGS_DEV) $(MANAGE) runserver 0.0.0.0:8000

celery:
	celery -A config.celery worker --loglevel=debug --concurrency=2

celery-beat:
	celery -A config.celery beat --loglevel=info --scheduler=django_celery_beat.schedulers:DatabaseScheduler

check-all: lint
	mypy apps/ config/ core/ --ignore-missing-imports
	pytest --cov=apps -v

generate-key:
	$(PYTHON) -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

superuser:
	DJANGO_SETTINGS_MODULE=$(SETTINGS_DEV) $(MANAGE) createsuperuser

# Mirrors what CI Lint does — ruff only. The frontend lives in its own
# repo and is built and published by that repo's GitHub Action.
prod-build:
	@echo "▶ Ruff (same gate as CI Lint)"
	ruff check .
	@echo ""
	@echo "✓ Prod build OK. The UI bundle is built and published by the UI repo's CI."

# Heavier: builds the actual prod Docker images locally. Needs
# .env.prod on disk. Use this when you want to verify Dockerfile or
# compose changes won't break the EC2 build.
prod-build-docker:
	@test -f .env.prod || (echo "✗ .env.prod missing — copy .env.prod.example and fill it in" && exit 1)
	docker compose -f docker/docker-compose.prod.yml build
