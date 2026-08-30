PYTHON ?= python3
export PYTHONPATH := .
LINT_FILES := artifacts.py adaptive_boundary.py experiment_engine.py experiment_index.py frequency_domain_metrics.py optimization_comparison.py optimization_engine.py optimization_recipe.py optimization_study.py robust_selection.py statistical_comparison.py statistical_engine.py study_recipe.py system_builder.py system_builder_routes waveform_metrics.py tests/support.py tests/test_artifacts.py tests/test_documentation.py tests/test_frequency_domain_metrics.py tests/test_system_builder.py tests/test_waveform_metrics.py

.PHONY: test lint typecheck ac transient nand sallen-key mixed-signal-daq sweep statistical-yield search step dashboard api-help system-builder

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(PYTHON) -m ruff check $(LINT_FILES)

typecheck:
	$(PYTHON) -m mypy artifacts.py waveform_metrics.py frequency_domain_metrics.py

ac:
	$(PYTHON) examples/analyze_rc.py

transient:
	$(PYTHON) examples/analyze_transient.py

nand:
	$(PYTHON) examples/analyze_nand.py

sallen-key:
	$(PYTHON) examples/analyze_sallen_key.py

mixed-signal-daq:
	$(PYTHON) examples/mixed_signal_daq_study.py

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

system-builder:
	$(PYTHON) system_builder.py
