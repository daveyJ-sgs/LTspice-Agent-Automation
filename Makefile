PYTHON ?= python3
export PYTHONPATH := .

.PHONY: test ac transient nand sallen-key sweep statistical-yield search step dashboard api-help

test:
	$(PYTHON) -m unittest discover -s tests -v

ac:
	$(PYTHON) examples/analyze_rc.py

transient:
	$(PYTHON) examples/analyze_transient.py

nand:
	$(PYTHON) examples/analyze_nand.py

sallen-key:
	$(PYTHON) examples/analyze_sallen_key.py

sweep:
	$(PYTHON) examples/sweep_rc.py

statistical-yield:
	$(PYTHON) examples/statistical_rc_yield.py

search:
	$(PYTHON) examples/design_search_rc.py

step:
	$(PYTHON) examples/analyze_step_rc.py

dashboard:
	$(PYTHON) report_runs.py

api-help:
	$(PYTHON) api_server.py --help
