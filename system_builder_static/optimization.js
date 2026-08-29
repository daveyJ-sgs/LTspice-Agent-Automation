"use strict";

let optimizationRecipe = null;
let optimizationTimer = null;
let optimizationSequence = 0;
let optimizationDisplayUnits = new WeakMap();
let latestOptimizationPreview = null;
let frozenOptimizationLaunch = null;
let trackedOptimizationJob = null;
let optimizationPollTimer = null;

const optId = (id) => document.getElementById(id);
const OPT_UNITS = {
  F: [["pF", "pF", 1e-12], ["nF", "nF", 1e-9], ["uF", "µF", 1e-6]],
  ohm: [["ohm", "Ω", 1], ["kohm", "kΩ", 1e3], ["Mohm", "MΩ", 1e6]],
};

function optNumber(value) {
  if (String(value).trim() === "") return "";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : value;
}

function optInput(value, label, onInput) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value ?? "";
  input.setAttribute("aria-label", label);
  input.addEventListener("input", () => onInput(input.value));
  return input;
}

function optSelect(value, choices, label, onChange) {
  const select = document.createElement("select");
  select.setAttribute("aria-label", label);
  for (const [key, text] of choices) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = text;
    select.append(option);
  }
  select.value = value;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function optField(caption, control) {
  const label = document.createElement("label");
  label.className = "field-caption";
  label.append(document.createTextNode(caption), control);
  return label;
}

function displayUnit(item) {
  if (!OPT_UNITS[item.unit]) return null;
  if (optimizationDisplayUnits.has(item)) return optimizationDisplayUnits.get(item);
  const values = item.values || [item.minimum, item.maximum];
  const magnitude = Math.max(...values.map(Number).filter(Number.isFinite).map(Math.abs), 0);
  const selected = item.unit === "F"
    ? (magnitude >= 1e-6 ? "uF" : magnitude >= 1e-9 ? "nF" : "pF")
    : (magnitude >= 1e6 ? "Mohm" : magnitude >= 1e3 ? "kohm" : "ohm");
  optimizationDisplayUnits.set(item, selected);
  return selected;
}

function unitFactor(item) {
  const selected = displayUnit(item);
  const choice = (OPT_UNITS[item.unit] || []).find(([key]) => key === selected);
  return choice ? choice[2] : 1;
}

function shown(value, factor) {
  const number = Number(value);
  return Number.isFinite(number) ? Number((number / factor).toPrecision(9)).toString() : value ?? "";
}

function stored(value, factor) {
  const parsed = optNumber(value);
  return typeof parsed === "number" ? Number((parsed * factor).toPrecision(15)) : parsed;
}

function scaledInput(value, factor, label, setter) {
  return optInput(shown(value, factor), label, (next) => {
    setter(stored(next, factor));
    scheduleOptimizationPreview();
  });
}

function setParameterKind(parameter, kind) {
  for (const key of ["minimum", "maximum", "count", "step", "values", "series"]) delete parameter[key];
  parameter.kind = kind;
  if (kind === "continuous") Object.assign(parameter, {minimum: 1, maximum: 2, count: 2});
  if (kind === "integer") Object.assign(parameter, {minimum: 1, maximum: 2, step: 1});
  if (kind === "categorical") parameter.values = ["option_a", "option_b"];
  if (kind === "preferred_values") Object.assign(parameter, {series: "E12", values: [1, 2]});
  if (kind === "preferred_series") Object.assign(parameter, {series: "E12", minimum: 1, maximum: 10});
}

function renderOptimizationDomains() {
  const parameters = optimizationRecipe.parameters || [];
  optId("optimization-domain-count").textContent = `${parameters.length} domains`;
  const rows = parameters.map((parameter, index) => {
    const row = document.createElement("tr");
    const name = optInput(parameter.name, `parameter ${index + 1} name`, (value) => {
      parameter.name = value;
      scheduleOptimizationPreview();
    });
    const kind = optSelect(parameter.kind, [
      ["continuous", "Continuous grid"],
      ["integer", "Integer range"],
      ["categorical", "Categories"],
      ["preferred_values", "Explicit preferred"],
      ["preferred_series", "Generated E-series"],
    ], `parameter ${parameter.name} domain type`, (value) => {
      setParameterKind(parameter, value);
      renderOptimizationDomains();
      scheduleOptimizationPreview();
    });
    const factor = unitFactor(parameter);
    const domain = document.createElement("div");
    domain.className = "domain-fields";
    const scaled = (key, caption) => optField(caption, scaledInput(
      parameter[key], factor, `${parameter.name} ${caption}`,
      (value) => { parameter[key] = value; },
    ));
    if (["continuous", "integer", "preferred_series"].includes(parameter.kind)) {
      domain.append(scaled("minimum", "Minimum"), scaled("maximum", "Maximum"));
      if (parameter.kind === "continuous") {
        domain.append(optField("Count", optInput(parameter.count, `${parameter.name} count`, (value) => {
          parameter.count = optNumber(value);
          scheduleOptimizationPreview();
        })));
      } else if (parameter.kind === "integer") {
        domain.append(scaled("step", "Step"));
      } else {
        domain.append(optField("Series", optSelect(parameter.series, [["E6", "E6"], ["E12", "E12"], ["E24", "E24"]], `${parameter.name} series`, (value) => {
          parameter.series = value;
          scheduleOptimizationPreview();
        })));
      }
    } else {
      domain.classList.add("values");
      if (parameter.kind === "preferred_values") {
        domain.append(optField("Series", optSelect(parameter.series, [["E6", "E6"], ["E12", "E12"], ["E24", "E24"]], `${parameter.name} series`, (value) => {
          parameter.series = value;
          scheduleOptimizationPreview();
        })));
      }
      const valueText = parameter.kind === "categorical"
        ? (parameter.values || []).join(", ")
        : (parameter.values || []).map((value) => shown(value, factor)).join(", ");
      domain.append(optField("Values", optInput(valueText, `${parameter.name} values`, (value) => {
        parameter.values = value.split(",").map((entry) => entry.trim()).filter(Boolean).map((entry) =>
          parameter.kind === "categorical" ? entry : stored(entry, factor));
        scheduleOptimizationPreview();
      })));
    }
    let unit;
    if (OPT_UNITS[parameter.unit]) {
      unit = optSelect(displayUnit(parameter), OPT_UNITS[parameter.unit].map(([key, label]) => [key, label]), `${parameter.name} display unit`, (value) => {
        optimizationDisplayUnits.set(parameter, value);
        renderOptimizationDomains();
      });
    } else {
      unit = optInput(parameter.unit, `${parameter.name} unit`, (value) => {
        parameter.unit = value;
        scheduleOptimizationPreview();
      });
    }
    for (const control of [name, kind, domain, unit]) {
      const cell = document.createElement("td");
      cell.append(control);
      row.append(cell);
    }
    return row;
  });
  optId("optimization-domains").replaceChildren(...rows);
  optId("optimization-fixed").textContent = Object.entries(optimizationRecipe.fixed_parameters || {})
    .map(([name, value]) => `${name}=${value}`).join(", ");
}

function renderOptimizationCorners() {
  const cards = (optimizationRecipe.corner_axes || []).map((axis, axisIndex) => {
    const card = document.createElement("section");
    card.className = "editor-card";
    const fields = document.createElement("div");
    fields.className = "compact-fields";
    for (const [key, caption] of [["name", "Axis name"], ["parameter", "Parameter"]]) {
      fields.append(optField(caption, optInput(axis[key], `corner ${axisIndex + 1} ${caption}`, (value) => {
        axis[key] = value;
        scheduleOptimizationPreview();
      })));
    }
    const factor = unitFactor(axis);
    let unit = OPT_UNITS[axis.unit]
      ? optSelect(displayUnit(axis), OPT_UNITS[axis.unit].map(([key, label]) => [key, label]), `corner ${axis.name} unit`, (value) => {
          optimizationDisplayUnits.set(axis, value);
          renderOptimizationCorners();
        })
      : optInput(axis.unit, `corner ${axis.name} unit`, (value) => { axis.unit = value; scheduleOptimizationPreview(); });
    fields.append(optField("Display unit", unit));
    const values = document.createElement("div");
    values.className = "corner-values";
    for (const [valueIndex, entry] of (axis.values || []).entries()) {
      const row = document.createElement("div");
      row.className = "corner-value-row";
      const label = optInput(entry.name, `${axis.name} value ${valueIndex + 1} name`, (value) => { entry.name = value; scheduleOptimizationPreview(); });
      const value = scaledInput(entry.value, factor, `${axis.name} ${entry.name} value`, (next) => { entry.value = next; });
      const marker = document.createElement("span");
      marker.className = "read-only-badge";
      marker.textContent = `C${valueIndex + 1}`;
      row.append(label, value, marker);
      values.append(row);
    }
    card.append(fields, values);
    return card;
  });
  optId("optimization-corners").replaceChildren(...cards);
}

function metricParametersText(selector) {
  return Object.entries(selector.metric_parameters || {}).map(([key, value]) => `${key}=${value}`).join(", ");
}

function setMetricParameters(selector, text) {
  const result = {};
  for (const entry of text.split(",").map((value) => value.trim()).filter(Boolean)) {
    const separator = entry.indexOf("=");
    if (separator < 1) {
      result[entry] = "";
      continue;
    }
    const key = entry.slice(0, separator).trim();
    result[key] = optNumber(entry.slice(separator + 1).trim());
  }
  if (Object.keys(result).length) selector.metric_parameters = result;
  else delete selector.metric_parameters;
}

function selectorField(item, key, caption, choices = null, numeric = false) {
  const update = (value) => {
    item[key] = numeric ? optNumber(value) : value;
    scheduleOptimizationPreview();
  };
  const control = choices
    ? optSelect(item[key], choices, `${item.name} ${caption}`, update)
    : optInput(item[key], `${item.name} ${caption}`, update);
  return optField(caption, control);
}

function renderOptimizationSelectors() {
  const objectives = optimizationRecipe.objectives || [];
  optId("optimization-objective-count").textContent = `${objectives.length} objectives`;
  optId("optimization-objectives").replaceChildren(...objectives.map((item) => {
    const row = document.createElement("div");
    row.className = "optimization-row";
    row.append(
      selectorField(item, "name", "Name"),
      selectorField(item, "experiment", "Study", [["ac", "AC"], ["transient", "Transient"]]),
      selectorField(item, "analysis", "Analysis"),
      selectorField(item, "metric", "Metric"),
      selectorField(item, "goal", "Goal", [["minimize", "Minimize"], ["maximize", "Maximize"]]),
      selectorField(item, "weight", "Weight", null, true),
      optField("Metric arguments", optInput(metricParametersText(item), `${item.name} metric arguments`, (value) => { setMetricParameters(item, value); scheduleOptimizationPreview(); })),
    );
    return row;
  }));

  const constraints = optimizationRecipe.constraints || [];
  optId("optimization-constraint-count").textContent = `${constraints.length} constraints`;
  optId("optimization-constraints").replaceChildren(...constraints.map((item) => {
    const row = document.createElement("div");
    row.className = "optimization-row constraint";
    row.append(
      selectorField(item, "name", "Name"),
      selectorField(item, "experiment", "Study", [["ac", "AC"], ["transient", "Transient"]]),
      selectorField(item, "analysis", "Analysis"),
      selectorField(item, "metric", "Metric"),
      selectorField(item, "operator", "Limit", [["<", "<"], ["<=", "≤"], [">", ">"], [">=", "≥"]]),
      selectorField(item, "target", "Target", null, true),
      optField("Metric arguments", optInput(metricParametersText(item), `${item.name} metric arguments`, (value) => { setMetricParameters(item, value); scheduleOptimizationPreview(); })),
    );
    return row;
  }));
}

function renderOptimizationEditors() {
  renderOptimizationDomains();
  renderOptimizationCorners();
  renderOptimizationSelectors();
}

function renderOptimizationErrors(errors = []) {
  const container = optId("optimization-errors");
  if (!errors.length) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const title = document.createElement("strong");
  title.textContent = "Optimization plan is not valid";
  const list = document.createElement("ul");
  for (const error of errors) {
    const item = document.createElement("li");
    item.textContent = `${error.path}: ${error.message}`;
    list.append(item);
  }
  container.replaceChildren(title, list);
  container.hidden = false;
}

function renderOptimizationPreview(result) {
  const status = optId("optimization-status");
  status.className = `status-pill ${result.valid ? "valid" : "invalid"}`;
  status.textContent = result.valid ? "Valid" : "Invalid";
  optId("optimization-limits").textContent = `${result.limits.maximum_candidates} candidates · ${result.limits.maximum_points} expanded points`;
  renderOptimizationErrors(result.errors || []);
  if (!result.valid) {
    latestOptimizationPreview = null;
    optId("optimization-freeze").disabled = true;
    optId("optimization-preview-title").textContent = "Definition needs attention";
    for (const id of ["optimization-candidates", "optimization-corner-count", "optimization-points", "optimization-runs"]) optId(id).textContent = "—";
    optId("optimization-plan-id").textContent = "Not generated";
    optId("optimization-domain-summary").replaceChildren();
    optId("optimization-experiments").replaceChildren();
    return;
  }
  latestOptimizationPreview = result;
  optId("optimization-freeze").disabled = false;
  optId("optimization-preview-title").textContent = "Ready to become immutable";
  optId("optimization-candidates").textContent = result.plan.candidate_count.toLocaleString();
  optId("optimization-corner-count").textContent = result.plan.corner_count.toLocaleString();
  optId("optimization-points").textContent = result.plan.point_count.toLocaleString();
  optId("optimization-runs").textContent = result.execution.total_run_count.toLocaleString();
  optId("optimization-plan-id").textContent = result.plan.plan_id;
  optId("optimization-policy").textContent = `${result.plan.selection_policy} · Preview writes nothing.`;
  const domains = Object.entries(result.plan.domain_sizes).map(([name, count]) => {
    const row = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = name;
    const value = document.createElement("strong");
    value.textContent = `${count} values`;
    row.append(label, value);
    return row;
  });
  optId("optimization-domain-summary").replaceChildren(...domains);
  const experiments = result.execution.experiments.map((name) => {
    const row = document.createElement("div");
    row.className = "experiment";
    const icon = document.createElement("span");
    icon.className = "experiment-icon";
    icon.textContent = name === "ac" ? "AC" : "TR";
    const detail = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = name === "ac" ? "Frequency response" : "Acquisition transient";
    const note = document.createElement("small");
    note.textContent = `${result.plan.objective_count} objectives · ${result.plan.constraint_count} total constraints`;
    detail.append(title, note);
    const runs = document.createElement("span");
    runs.className = "run-count";
    runs.textContent = `${result.plan.point_count} runs`;
    row.append(icon, detail, runs);
    return row;
  });
  optId("optimization-experiments").replaceChildren(...experiments);
}

function invalidateOptimizationLaunch() {
  frozenOptimizationLaunch = null;
  optId("optimization-confirmation").hidden = true;
  optId("optimization-acknowledgement").checked = false;
  optId("optimization-acknowledgement").disabled = false;
  optId("optimization-start").disabled = true;
}

function scheduleOptimizationPreview() {
  if (!optimizationRecipe) return;
  invalidateOptimizationLaunch();
  window.clearTimeout(optimizationTimer);
  const status = optId("optimization-status");
  status.className = "status-pill idle preview-pending";
  status.textContent = "Checking";
  optimizationTimer = window.setTimeout(previewOptimization, 350);
}

async function freezeOptimizationPlan() {
  if (!optimizationRecipe || !latestOptimizationPreview) return;
  const button = optId("optimization-freeze");
  button.disabled = true;
  button.textContent = "Publishing…";
  try {
    const response = await fetch("/api/optimization/freeze", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"},
      body: JSON.stringify({
        recipe: optimizationRecipe,
        expected_recipe_sha256: latestOptimizationPreview.recipe.sha256,
        expected_plan_id: latestOptimizationPreview.plan.plan_id,
        expected_point_count: latestOptimizationPreview.plan.point_count,
        expected_total_run_count: latestOptimizationPreview.execution.total_run_count,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Optimization plan could not be published");
    frozenOptimizationLaunch = result;
    optId("optimization-frozen-plan").textContent = result.plan.plan_id;
    optId("optimization-confirm-candidates").textContent = result.plan.candidate_count.toLocaleString();
    optId("optimization-confirm-corners").textContent = latestOptimizationPreview.plan.corner_count.toLocaleString();
    optId("optimization-confirm-points").textContent = result.plan.point_count.toLocaleString();
    optId("optimization-confirm-runs").textContent = result.execution.total_run_count.toLocaleString();
    optId("optimization-confirm-concurrency").textContent = result.execution.max_concurrency.toLocaleString();
    optId("optimization-frozen-artifact").textContent = result.plan.artifact;
    optId("optimization-acknowledgement").checked = false;
    optId("optimization-acknowledgement").disabled = false;
    optId("optimization-start").disabled = true;
    optId("optimization-confirmation").hidden = false;
    renderOptimizationErrors([]);
  } catch (error) {
    renderOptimizationErrors([{path: "publication", message: error.message}]);
  } finally {
    button.disabled = latestOptimizationPreview === null;
    button.textContent = "Publish confirmed plan";
  }
}

function optimizationStatusClass(status) {
  if (status === "completed") return "valid";
  if (["failed", "cancelled"].includes(status)) return "invalid";
  return "idle";
}

function optimizationProgressRow(label, value, total) {
  const row = document.createElement("div");
  row.className = "optimization-progress-row";
  const text = document.createElement("span");
  text.textContent = label;
  const count = document.createElement("strong");
  count.textContent = `${Number(value).toLocaleString()} / ${Number(total).toLocaleString()}`;
  const track = document.createElement("div");
  track.className = "progress-track";
  const fill = document.createElement("span");
  fill.style.width = `${total ? Math.min(100, (Number(value) / Number(total)) * 100) : 0}%`;
  track.append(fill);
  row.append(text, count, track);
  return row;
}

function renderOptimizationJob(job) {
  trackedOptimizationJob = job;
  const container = optId("optimization-job");
  const heading = document.createElement("div");
  heading.className = "panel-heading compact";
  const title = document.createElement("div");
  const step = document.createElement("p");
  step.className = "step";
  step.textContent = "DURABLE OPTIMIZATION JOB";
  const name = document.createElement("h3");
  name.textContent = job.optimization_job_id;
  title.append(step, name);
  const status = document.createElement("span");
  status.className = `status-pill ${optimizationStatusClass(job.status)}`;
  status.textContent = job.status;
  heading.append(title, status);

  const structure = document.createElement("p");
  structure.className = "optimization-structure";
  structure.textContent = `${job.progress.candidate_count} candidates × ${job.progress.corner_count} corners × ${job.experiments.length} analyses`;
  const progress = document.createElement("div");
  progress.className = "optimization-progress";
  progress.append(optimizationProgressRow("Total LTspice runs", job.progress.finished_points, job.progress.total_runs));
  for (const child of job.experiments) {
    progress.append(optimizationProgressRow(`${child.name.toUpperCase()} · ${child.status}`, child.finished_points, child.point_count));
  }
  const evaluation = document.createElement("p");
  evaluation.className = "editor-note";
  evaluation.textContent = job.progress.evaluation === "complete"
    ? "Electrical analysis is complete. Pareto and winner visualization begins in GUI-C3."
    : `Optimization evaluation: ${job.progress.evaluation}.`;
  const actions = document.createElement("div");
  actions.className = "job-actions";
  if (["defined", "queued", "running", "cancelling"].includes(job.status)) {
    const cancel = document.createElement("button");
    cancel.className = "secondary-button";
    cancel.type = "button";
    cancel.textContent = "Cancel remaining runs";
    cancel.addEventListener("click", () => mutateOptimizationJob("cancel"));
    actions.append(cancel);
  } else if (job.resumable) {
    const resume = document.createElement("button");
    resume.className = "secondary-button";
    resume.type = "button";
    resume.textContent = "Resume unfinished runs";
    resume.addEventListener("click", () => mutateOptimizationJob("resume"));
    actions.append(resume);
  }
  if (job.error) {
    const error = document.createElement("p");
    error.className = "job-error";
    error.textContent = job.error;
    actions.append(error);
  }
  container.replaceChildren(heading, structure, progress, evaluation, actions);
  container.hidden = false;
  window.clearTimeout(optimizationPollTimer);
  if (["defined", "queued", "running", "cancelling"].includes(job.status)) {
    optimizationPollTimer = window.setTimeout(pollOptimizationJob, 750);
  }
}

async function pollOptimizationJob() {
  if (!trackedOptimizationJob) return;
  try {
    const response = await fetch(`/api/optimization/jobs/${trackedOptimizationJob.optimization_job_id}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Optimization status is unavailable");
    renderOptimizationJob(result);
  } catch (error) {
    renderOptimizationErrors([{path: "optimization job", message: error.message}]);
  }
}

async function mutateOptimizationJob(action) {
  if (!trackedOptimizationJob) return;
  try {
    const response = await fetch(`/api/optimization/jobs/${trackedOptimizationJob.optimization_job_id}/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"},
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || `Optimization ${action} failed`);
    renderOptimizationJob(result);
  } catch (error) {
    renderOptimizationErrors([{path: `optimization ${action}`, message: error.message}]);
  }
}

async function startOptimization() {
  if (!optimizationRecipe || !frozenOptimizationLaunch || !optId("optimization-acknowledgement").checked) return;
  const button = optId("optimization-start");
  button.disabled = true;
  button.textContent = "Queuing…";
  try {
    const response = await fetch("/api/optimization/start", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"},
      body: JSON.stringify({
        launch_token: frozenOptimizationLaunch.launch_token,
        recipe: optimizationRecipe,
        confirmed_point_count: frozenOptimizationLaunch.plan.point_count,
        confirmed_run_count: frozenOptimizationLaunch.execution.total_run_count,
        acknowledged: true,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Optimization could not be started");
    optId("optimization-acknowledgement").disabled = true;
    button.textContent = "Optimization queued";
    renderOptimizationJob(result);
  } catch (error) {
    renderOptimizationErrors([{path: "execution", message: error.message}]);
    button.disabled = false;
    button.textContent = "Start local optimization";
  }
}

async function recoverOptimizationJob() {
  const response = await fetch("/api/optimization/jobs?limit=1");
  if (!response.ok) return;
  const result = await response.json();
  if (result.jobs.length) renderOptimizationJob(result.jobs[0]);
}

async function previewOptimization() {
  if (!optimizationRecipe) return;
  const sequence = ++optimizationSequence;
  const button = optId("optimization-preview");
  button.disabled = true;
  button.textContent = "Resolving…";
  try {
    const response = await fetch("/api/optimization/preview", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"},
      body: JSON.stringify(optimizationRecipe),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Optimization preview failed");
    if (sequence === optimizationSequence) renderOptimizationPreview(result);
  } catch (error) {
    if (sequence === optimizationSequence) renderOptimizationPreview({
      valid: false,
      errors: [{path: "optimization", message: error.message}],
      limits: {maximum_candidates: 512, maximum_points: 1000},
    });
  } finally {
    if (sequence === optimizationSequence) {
      button.disabled = false;
      button.textContent = "Preview candidate plan";
    }
  }
}

async function loadOptimizationReference() {
  const response = await fetch("/api/examples/mixed-signal-daq-optimization");
  if (!response.ok) throw new Error("DAQ optimization reference could not be loaded");
  optimizationRecipe = await response.json();
  optimizationDisplayUnits = new WeakMap();
  renderOptimizationEditors();
  await previewOptimization();
  await recoverOptimizationJob();
}

optId("optimization-preview").addEventListener("click", previewOptimization);
optId("optimization-freeze").addEventListener("click", freezeOptimizationPlan);
optId("optimization-acknowledgement").addEventListener("change", () => {
  optId("optimization-start").disabled = !optId("optimization-acknowledgement").checked;
});
optId("optimization-start").addEventListener("click", startOptimization);
optId("optimization-file").addEventListener("change", async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  try {
    optimizationRecipe = JSON.parse(await file.text());
    optimizationDisplayUnits = new WeakMap();
    renderOptimizationEditors();
    await previewOptimization();
  } catch (error) {
    renderOptimizationPreview({
      valid: false,
      errors: [{path: "optimization", message: `Could not load recipe: ${error.message}`}],
      limits: {maximum_candidates: 512, maximum_points: 1000},
    });
  }
  event.target.value = "";
});
optId("optimization-save").addEventListener("click", () => {
  if (!optimizationRecipe) return;
  const blob = new Blob([`${JSON.stringify(optimizationRecipe, null, 2)}\n`], {type: "application/json"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "mixed-signal-daq.ltopt.json";
  link.click();
  URL.revokeObjectURL(link.href);
});
optId("optimization-reset").addEventListener("click", () => {
  loadOptimizationReference().catch((error) => renderOptimizationPreview({
    valid: false,
    errors: [{path: "optimization", message: error.message}],
    limits: {maximum_candidates: 512, maximum_points: 1000},
  }));
});

loadOptimizationReference().catch((error) => renderOptimizationPreview({
  valid: false,
  errors: [{path: "optimization", message: error.message}],
  limits: {maximum_candidates: 512, maximum_points: 1000},
}));
