all: clean install test

install:
	poetry install --with dev

lint:
	poetry run flake8 zfs_agent tests --count --show-source --statistics

pre-commit:
	poetry run pre-commit install
	poetry run pre-commit run -a

typecheck:
	poetry run mypy --strict zfs_agent

test:
	poetry run pytest -v --capture=sys --cov=zfs_agent --cov-report lcov

test-docker:
	docker build -t zfs-agent-integration -f tests/docker/Dockerfile .
	docker run --rm --privileged zfs-agent-integration

build:
	poetry build

clean:
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -f {} +
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +
