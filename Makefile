.DEFAULT_GOAL := help
TOX = ''
.PHONY: help clean piptools requirements dev_requirements \
        doc_requirements prod_requirements static shell test coverage \
        isort_check isort style lint quality pii_check validate \
        migrate html_coverage upgrade extract_translation dummy_translations \
        compile_translations fake_translations  pull_translations \
        push_translations start-devstack open-devstack  pkg-devstack \
        detect_changed_source_translations validate_translations \
        dev.provision dev.init dev.makemigrations dev.migrate dev.up \
        dev.up.build dev.down dev.destroy dev.stop docker_build \
        shellcheck check_keywords install_transifex_client


define BROWSER_PYSCRIPT
import os, webbrowser, sys
try:
	from urllib import pathname2url
except:
	from urllib.request import pathname2url

webbrowser.open("file://" + pathname2url(os.path.abspath(sys.argv[1])))
endef
export BROWSER_PYSCRIPT
BROWSER := python3 -c "$$BROWSER_PYSCRIPT"

ifdef TOXENV
TOX := tox -- #to isolate each tox environment if TOXENV is defined
endif

help: ## display this help message
	@echo "Please use \`make <target>' where <target> is one of"
	@perl -nle'print $& if m{^[\.a-zA-Z_-]+:.*?## .*$$}' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m  %-25s\033[0m %s\n", $$1, $$2}'

clean: ## delete generated byte code and coverage reports
	find . -name '*.pyc' -delete
	coverage erase
	rm -rf assets
	rm -rf pii_report

piptools: ## install pinned version of pip-compile and pip-sync
	pip install -r requirements/pip-tools.txt

requirements: piptools dev_requirements ## sync to default requirements

dev_requirements: ## sync to requirements for local development
	pip-sync -q requirements/dev.txt

doc_requirements:
	pip-sync -q requirements/doc.txt

production-requirements: piptools ## install requirements for production
	pip-sync -q requirements/production.txt

static: ## generate static files
	python3 manage.py collectstatic --noinput

shell: ## run Django shell
	python3 manage.py shell

test: clean ## run tests and generate coverage report
	$(TOX)python3 -Wd -m pytest

# To be run from CI context
coverage: clean
	pytest --cov-report html
	$(BROWSER) htmlcov/index.html

isort_check: ## check that isort has been run
	isort --check-only --diff enterprise_catalog/

isort: ## run isort to sort imports in all Python files
	isort --atomic enterprise_catalog/

style: ## run Python style checker
	pycodestyle enterprise_catalog *.py

lint: ## run Python code linting
	pylint --rcfile=pylintrc enterprise_catalog *.py

quality: clean style isort_check lint ## check code style and import sorting, then lint

pii_check: ## check for PII annotations on all Django models
	DJANGO_SETTINGS_MODULE=enterprise_catalog.settings.test \
	code_annotations django_find_annotations --config_file .pii_annotations.yml --lint --report --coverage

validate: test quality pii_check ## run tests, quality, and PII annotation checks

migrate: ## apply database migrations
	python3 manage.py migrate

html_coverage: ## generate and view HTML coverage report
	coverage html && open htmlcov/index.html

upgrade: export CUSTOM_COMPILE_COMMAND=make upgrade
upgrade: piptools ## update the requirements/*.txt files with the latest packages satisfying requirements/*.in
	# Make sure to compile files after any other files they include!
	pip-compile --allow-unsafe --rebuild --upgrade -o requirements/pip.txt requirements/pip.in
	pip-compile --upgrade -o requirements/pip-tools.txt requirements/pip-tools.in
	pip install -qr requirements/pip.txt
	pip install -qr requirements/pip-tools.txt
	pip-compile --upgrade -o requirements/base.txt requirements/base.in
	pip-compile --upgrade -o requirements/test.txt requirements/test.in
	pip-compile --upgrade -o requirements/doc.txt requirements/doc.in
	pip-compile --upgrade -o requirements/quality.txt requirements/quality.in
	pip-compile --upgrade -o requirements/validation.txt requirements/validation.in
	pip-compile --upgrade -o requirements/dev.txt requirements/dev.in
	pip-compile --upgrade -o requirements/production.txt requirements/production.in
	# Let tox control the Django version for tests
	grep -e "^django==" requirements/base.txt > requirements/django.txt
	sed '/^[dD]jango==/d' requirements/test.txt > requirements/test.tmp
	mv requirements/test.tmp requirements/test.txt

extract_translations: ## extract strings to be translated, outputting .mo files
	python3 manage.py makemessages -l en -v1 -d django
	python3 manage.py makemessages -l en -v1 -d djangojs

dummy_translations: ## generate dummy translation (.po) files
	cd enterprise_catalog && i18n_tool dummy

compile_translations: # compile translation files, outputting .po files for each supported language
	python3 manage.py compilemessages

fake_translations: ## generate and compile dummy translation files

pull_translations: ## pull translations from Transifex
	tx pull -af --mode reviewed

push_translations: ## push source translation files (.po) from Transifex
	tx push -s

start-devstack: ## run a local development copy of the server
	docker-compose --x-networking up

open-devstack: ## open a shell on the server started by start-devstack
	docker exec -it enterprise_catalog /edx/app/catalog/devstack.sh open

pkg-devstack: ## build the catalog image from the latest configuration and code
	docker build -t enterprise_catalog:latest -f docker/build/enterprise_catalog/Dockerfile git://github.com/edx/configuration

detect_changed_source_translations: ## check if translation files are up-to-date
	cd enterprise_catalog && i18n_tool changed

validate_translations: fake_translations detect_changed_source_translations ## install fake translations and check if translation files are up-to-date

# Docker commands below
dev.provision:
	bash ./provision-catalog.sh

dev.init: dev.up dev.migrate # start the docker container and run migrations

dev.makemigrations:
	docker exec -it enterprise.catalog.app bash -c 'cd /edx/app/enterprise_catalog/enterprise_catalog && python3 manage.py makemigrations'

dev.migrate: # Migrates databases. Application and DB server must be up for this to work.
	docker exec -it enterprise.catalog.app bash -c 'cd /edx/app/enterprise_catalog/enterprise_catalog && make migrate'

dev.up: dev.up.redis # Starts all containers
	docker-compose up -d

dev.up.build: # Runs docker-compose -up -d --build
	docker-compose up -d --build

dev.up.redis:
	docker-compose -f $(DEVSTACK_WORKSPACE)/devstack/docker-compose.yml up -d redis

dev.down: ## Kills containers and all of their data that isn't in volumes
	docker-compose down

dev.destroy: dev.down # Kills containers and destroys volumes. If you get an error after running this, also run: docker volume rm portal-designer_designer_mysql
	docker volume rm enterprise-catalog_enterprise_catalog_mysql

dev.stop: # Stops containers so they can be restarted
	docker-compose stop

dev.backup: dev.up
	docker run --rm --volumes-from enterprise.catalog.mysql -v $$(pwd)/.dev/backups:/backup debian:jessie tar zcvf /backup/mysql.tar.gz /var/lib/mysql

dev.restore: dev.up
	docker run --rm --volumes-from enterprise.catalog.mysql -v $$(pwd)/.dev/backups:/backup debian:jessie tar zxvf /backup/mysql.tar.gz

mysql-client:  # Opens mysql client in the mysql container shell
	docker-compose exec -u 0 mysql env TERM=$(TERM) mysql enterprise_catalog

%-shell: ## Run a shell, as root, on the specified service container
	docker-compose exec -u 0 $* env TERM=$(TERM) bash

%-logs: ## View the logs of the specified service container
	docker-compose logs -f --tail=500 $*

%-restart: # Restart the specified service container
	docker-compose restart $*

app-restart-devserver: ## Kill the enterprise-catalog development server. Watcher should restart it.
	docker-compose exec app bash -c 'kill $$(ps aux | egrep "manage.py ?\w* runserver" | egrep -v "while|grep" | awk "{print \$$2}")'

%-attach: ## Attach terminal I/O to the specified service container
	docker attach enterprise.catalog.$*

docker_build: ## Builds with the latest enterprise catalog
	docker build . --target app -t openedx/enterprise-catalog:latest
	docker build . --target newrelic -t openedx/enterprise-catalog:latest-newrelic

docker_tag: docker_build
	docker tag openedx/enterprise-catalog openedx/enterprise-catalog:$$GITHUB_SHA
	docker tag openedx/enterprise-catalog:latest-newrelic openedx/enterprise-catalog:$$GITHUB_SHA-newrelic

docker_auth:
	echo "$$DOCKERHUB_PASSWORD" | docker login -u "$$DOCKERHUB_USERNAME" --password-stdin

docker_push: docker_tag docker_auth ## push to docker hub
	docker push 'openedx/enterprise-catalog:latest'
	docker push "openedx/enterprise-catalog:$$GITHUB_SHA"
	docker push 'openedx/enterprise-catalog:latest-newrelic'
	docker push "openedx/enterprise-catalog:$$GITHUB_SHA-newrelic"

check_keywords: ## Scan the Django models in all installed apps in this project for restricted field names
	python manage.py check_reserved_keywords --override_file db_keyword_overrides.yml

install_transifex_client: ## Install the Transifex client
	curl -o- https://raw.githubusercontent.com/transifex/cli/master/install.sh | bash
	git checkout -- LICENSE README.md
