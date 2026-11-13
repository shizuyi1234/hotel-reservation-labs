PYTHON ?= python3

.PHONY: build test demo run

build:
	$(PYTHON) -m compileall -q hotel

test: build
	$(PYTHON) -m unittest discover -s tests -v

demo: build
	$(PYTHON) demo.py

run: build
	$(PYTHON) -m hotel

.PHONY: web
web: build
	$(PYTHON) -m hotel.web
