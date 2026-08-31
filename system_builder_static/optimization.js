"use strict";

let optimizationRecipe = null;
let optimizationTimer = null;
let optimizationSequence = 0;
let optimizationDisplayUnits = new WeakMap();
let latestOptimizationPreview = null;
let frozenOptimizationLaunch = null;
let trackedOptimizationJob = null;
let optimizationPollTimer = null;
let displayedOptimizationStudy = null;
let selectedQualificationSource = null;
let latestQualificationPreview = null;
let frozenQualificationLaunch = null;
let trackedQualificationJob = null;
let qualificationPollTimer = null;
let displayedQualificationStudy = null;

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

function optimizationLabel(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function optimizationEngineeringValue(value, unit = "") {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? "—");
  const scales = unit === "F"
    ? [[1e-6, "µF"], [1e-9, "nF"], [1e-12, "pF"]]
    : unit === "ohm"
      ? [[1e6, "MΩ"], [1e3, "kΩ"], [1, "Ω"]]
      : unit === "s"
        ? [[1, "s"], [1e-3, "ms"], [1e-6, "µs"], [1e-9, "ns"]]
        : unit === "Hz"
          ? [[1e9, "GHz"], [1e6, "MHz"], [1e3, "kHz"], [1, "Hz"]]
          : null;
  if (scales) {
    const magnitude = Math.abs(number);
    const [factor, label] = scales.find(([candidate]) => magnitude >= candidate) || scales.at(-1);
    return `${Number((number / factor).toPrecision(4))} ${label}`;
  }
  const suffix = unit ? ` ${unit}` : "";
  return `${Number(number.toPrecision(4))}${suffix}`;
}

function optimizationValueNode(value, unit = "") {
  const node = document.createElement("span");
  node.textContent = optimizationEngineeringValue(value, unit);
  node.title = `Exact: ${value}${unit ? ` ${unit}` : ""}`;
  return node;
}

function optimizationRecordText(records) {
  return Object.entries(records || {}).map(([name, record]) =>
    `${optimizationLabel(name)} ${optimizationEngineeringValue(record.value, record.unit)}`
  ).join(" · ");
}

function renderSelectedOptimizationCandidate(result, candidate) {
  const title = optId("optimization-selected-title");
  if (!candidate) {
    title.textContent = "No feasible candidate selected";
    optId("optimization-selected-parameters").replaceChildren();
    optId("optimization-selected-objectives").replaceChildren();
    optId("optimization-selected-constraints").replaceChildren();
    return;
  }
  title.textContent = `Candidate ${candidate.candidate_index}`;
  const parameters = Object.entries(candidate.parameters || {}).map(([name, value]) => {
    const item = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = name;
    const formatted = optimizationValueNode(value, result.parameter_units?.[name] || "");
    item.append(label, formatted);
    return item;
  });
  optId("optimization-selected-parameters").replaceChildren(...parameters);

  const objectives = Object.entries(candidate.objectives || {}).map(([name, record]) => {
    const item = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = optimizationLabel(name);
    const value = optimizationValueNode(record.value, record.unit);
    item.append(label, value);
    return item;
  });
  optId("optimization-selected-objectives").replaceChildren(...objectives);

  const constraints = Object.entries(candidate.constraints || {}).sort((left, right) => {
    const leftTarget = Math.max(Math.abs(Number(left[1].target)), 1e-30);
    const rightTarget = Math.max(Math.abs(Number(right[1].target)), 1e-30);
    return Number(left[1].margin) / leftTarget - Number(right[1].margin) / rightTarget;
  }).map(([name, record]) => {
    const row = document.createElement("div");
    row.className = `constraint-result ${record.passed ? "passed" : "failed"}`;
    const identity = document.createElement("div");
    const label = document.createElement("strong");
    label.textContent = optimizationLabel(name);
    const requirement = document.createElement("small");
    requirement.textContent = `${optimizationEngineeringValue(record.worst_value, record.unit)} ${record.operator} ${optimizationEngineeringValue(record.target, record.unit)}`;
    identity.append(label, requirement);
    const margin = document.createElement("span");
    margin.textContent = `${record.passed ? "+" : ""}${optimizationEngineeringValue(record.margin, record.unit)} margin`;
    margin.title = `Worst planned point ${record.worst_point_index}`;
    row.append(identity, margin);
    return row;
  });
  optId("optimization-selected-constraints").replaceChildren(...constraints);
}

function optimizationSvgElement(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function renderOptimizationParetoPlot(result) {
  const container = optId("optimization-pareto-plot");
  const objectives = result.objectives || [];
  const candidates = (result.candidates || []).filter((candidate) => candidate.status === "feasible");
  if (objectives.length !== 2 || !candidates.length) {
    const empty = document.createElement("p");
    empty.className = "editor-empty";
    empty.textContent = "Two complete objectives and at least one feasible candidate are required for the tradeoff plot.";
    container.replaceChildren(empty);
    return;
  }
  const [xObjective, yObjective] = objectives;
  const points = candidates.map((candidate) => ({
    candidate,
    x: Number(candidate.objectives[xObjective.name].value),
    y: Number(candidate.objectives[yObjective.name].value),
  })).filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
  if (!points.length) return;
  const width = 820;
  const height = 390;
  const bounds = {left: 78, right: 28, top: 28, bottom: 64};
  const xValues = points.map((point) => point.x);
  const yValues = points.map((point) => point.y);
  let xMin = Math.min(...xValues); let xMax = Math.max(...xValues);
  let yMin = Math.min(...yValues); let yMax = Math.max(...yValues);
  if (xMin === xMax) { xMin -= 1; xMax += 1; }
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const xPad = (xMax - xMin) * 0.08;
  const yPad = (yMax - yMin) * 0.08;
  xMin -= xPad; xMax += xPad; yMin -= yPad; yMax += yPad;
  const xPosition = (value) => bounds.left + ((value - xMin) / (xMax - xMin)) * (width - bounds.left - bounds.right);
  const yPosition = (value) => height - bounds.bottom - ((value - yMin) / (yMax - yMin)) * (height - bounds.top - bounds.bottom);
  const svg = optimizationSvgElement("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Optimization objective tradeoff plot"});
  svg.classList.add("optimization-pareto-svg");
  for (let index = 0; index <= 4; index += 1) {
    const x = bounds.left + index * (width - bounds.left - bounds.right) / 4;
    const y = bounds.top + index * (height - bounds.top - bounds.bottom) / 4;
    svg.append(
      optimizationSvgElement("line", {x1: x, y1: bounds.top, x2: x, y2: height - bounds.bottom, class: "plot-grid"}),
      optimizationSvgElement("line", {x1: bounds.left, y1: y, x2: width - bounds.right, y2: y, class: "plot-grid"}),
    );
    const xTick = optimizationSvgElement("text", {x, y: height - bounds.bottom + 22, class: "plot-tick", "text-anchor": "middle"});
    xTick.textContent = optimizationEngineeringValue(xMin + index * (xMax - xMin) / 4, points[0].candidate.objectives[xObjective.name].unit);
    const yTick = optimizationSvgElement("text", {x: bounds.left - 10, y: y + 4, class: "plot-tick", "text-anchor": "end"});
    yTick.textContent = optimizationEngineeringValue(yMax - index * (yMax - yMin) / 4, points[0].candidate.objectives[yObjective.name].unit);
    svg.append(xTick, yTick);
  }
  svg.append(
    optimizationSvgElement("line", {x1: bounds.left, y1: height - bounds.bottom, x2: width - bounds.right, y2: height - bounds.bottom, class: "plot-axis"}),
    optimizationSvgElement("line", {x1: bounds.left, y1: bounds.top, x2: bounds.left, y2: height - bounds.bottom, class: "plot-axis"}),
  );
  const xLabel = optimizationSvgElement("text", {x: (bounds.left + width - bounds.right) / 2, y: height - 16, class: "plot-label", "text-anchor": "middle"});
  xLabel.textContent = `${optimizationLabel(xObjective.name)} · ${xObjective.goal}`;
  const yLabel = optimizationSvgElement("text", {x: 18, y: (bounds.top + height - bounds.bottom) / 2, class: "plot-label", "text-anchor": "middle", transform: `rotate(-90 18 ${(bounds.top + height - bounds.bottom) / 2})`});
  yLabel.textContent = `${optimizationLabel(yObjective.name)} · ${yObjective.goal}`;
  svg.append(xLabel, yLabel);
  for (const point of points) {
    const circle = optimizationSvgElement("circle", {
      cx: xPosition(point.x), cy: yPosition(point.y), r: point.candidate.selected ? 7 : 5,
      class: point.candidate.selected ? "selected" : point.candidate.pareto ? "pareto" : "feasible",
      tabindex: "0",
    });
    const title = optimizationSvgElement("title");
    title.textContent = `Candidate ${point.candidate.candidate_index}: ${optimizationRecordText(point.candidate.objectives)}`;
    circle.append(title);
    svg.append(circle);
  }
  container.replaceChildren(svg);
}

function renderOptimizationCandidates(result) {
  const rows = (result.candidates || []).map((candidate) => {
    const row = document.createElement("tr");
    if (candidate.selected) row.className = "selected-row";
    const index = document.createElement("td");
    index.textContent = candidate.selected ? `★ ${candidate.candidate_index}` : candidate.candidate_index;
    const status = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `candidate-status ${candidate.status}`;
    badge.textContent = candidate.selected ? "Selected" : candidate.pareto ? "Pareto" : optimizationLabel(candidate.status);
    status.append(badge);
    const design = document.createElement("td");
    design.textContent = Object.entries(candidate.parameters || {}).map(([name, value]) =>
      `${name}=${optimizationEngineeringValue(value, result.parameter_units?.[name] || "")}`
    ).join(", ");
    const objectives = document.createElement("td");
    objectives.textContent = optimizationRecordText(candidate.objectives);
    const decision = document.createElement("td");
    const failed = Object.entries(candidate.constraints || {}).filter(([, record]) => !record.passed);
    decision.textContent = candidate.errors?.length
      ? candidate.errors.join(" · ")
      : failed.length
        ? failed.map(([name, record]) => `${optimizationLabel(name)} (${optimizationEngineeringValue(record.margin, record.unit)} margin)`).join(" · ")
        : candidate.selected
          ? `Winner · score ${Number(candidate.selection_score).toPrecision(4)}`
          : candidate.pareto ? "Nondominated alternative" : "Feasible; dominated in objective space";
    row.append(index, status, design, objectives, decision);
    return row;
  });
  optId("optimization-candidate-rows").replaceChildren(...rows);
  optId("optimization-candidate-summary").textContent = `Candidate evidence (${rows.length})`;
}

// Qualification is a peer top-level view now rather than a panel nested
// inside optimization results, so "a candidate is ready to qualify" has to
// drive several independent pieces of UI in one place: the empty state vs.
// the real panel inside the qualification view, the nav badge, the
// cross-link button on the optimization results, and the dashboard's
// attention note.
function setQualificationAvailability(available) {
  optId("qualification-panel").hidden = !available;
  optId("qualification-empty").hidden = available;
  optId("goto-qualification-link").hidden = !available;
  optId("qualification-nav-badge").hidden = !available;
  optId("dashboard-attention").hidden = !available;
}

function renderOptimizationResults(result) {
  displayedOptimizationStudy = result.study_id;
  optId("optimization-results-title").textContent = `Decision · ${result.study_id}`;
  optId("optimization-selection-explanation").textContent = result.selection_explanation;
  const metrics = [
    ["Candidates", result.candidate_count],
    ["Feasible", result.feasible_candidates],
    ["Rejected / invalid", `${result.constraint_failed_candidates} / ${result.invalid_candidates}`],
    ["Pareto", result.pareto_candidates],
  ].map(([labelText, valueText]) => {
    const item = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = labelText;
    const value = document.createElement("strong");
    value.textContent = valueText;
    item.append(label, value);
    return item;
  });
  optId("optimization-result-metrics").replaceChildren(...metrics);
  const selected = (result.candidates || []).find((candidate) => candidate.selected) || null;
  selectedQualificationSource = selected ? {study_id: result.study_id, candidate_index: selected.candidate_index} : null;
  setQualificationAvailability(selectedQualificationSource !== null);
  renderSelectedOptimizationCandidate(result, selected);
  renderOptimizationParetoPlot(result);
  renderOptimizationCandidates(result);
  const links = Object.entries(result.evidence || {}).map(([name, url]) => {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = name === "report" ? "Full HTML report" : name.toUpperCase();
    return link;
  });
  optId("optimization-evidence-links").replaceChildren(...links);
  optId("optimization-results").hidden = false;
  recoverQualificationJob().catch(() => {});
}

function qualificationRequest() {
  return {
    ...selectedQualificationSource,
    sample_count: Number(optId("qualification-samples").value),
    seed: Number(optId("qualification-seed").value),
  };
}

function qualificationErrors(messages) {
  const box = optId("qualification-errors");
  box.hidden = messages.length === 0;
  box.replaceChildren(...messages.map((message) => {
    const item = document.createElement("p"); item.textContent = message; return item;
  }));
}

function renderQualificationModel(result) {
  const rows = result.plan.variables.map((variable) => {
    const row = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = variable.name;
    const value = document.createElement("span");
    value.textContent = `${optimizationEngineeringValue(variable.nominal, variable.unit)} nominal · σ ${optimizationEngineeringValue(variable.sigma, variable.unit)} · ${optimizationEngineeringValue(variable.minimum, variable.unit)} to ${optimizationEngineeringValue(variable.maximum, variable.unit)}`;
    row.append(name, value); return row;
  });
  // Every named corner axis, using its own name/unit rather than a single
  // hardcoded axis and label — the previous version assumed corner_axes[0]
  // always existed and was always "ADC load", which threw on any plan with
  // zero corner axes and mislabeled every plan with a different one.
  for (const axis of result.plan.corner_axes || []) {
    const corner = document.createElement("div");
    const cornerName = document.createElement("strong"); cornerName.textContent = optimizationLabel(axis.name || "corner axis");
    const cornerValues = document.createElement("span");
    cornerValues.textContent = (axis.values || []).map((item) => `${optimizationLabel(item.name)} ${optimizationEngineeringValue(item.value, axis.unit)}`).join(" · ");
    corner.append(cornerName, cornerValues); rows.push(corner);
  }
  optId("qualification-model").replaceChildren(...rows);
}

async function previewQualification() {
  if (!selectedQualificationSource) return;
  frozenQualificationLaunch = null;
  optId("qualification-confirmation").hidden = true;
  const button = optId("qualification-preview"); button.disabled = true; button.textContent = "Resolving…";
  try {
    const response = await fetch("/api/qualification/preview", {method: "POST", headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"}, body: JSON.stringify(qualificationRequest())});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Qualification preview failed");
    latestQualificationPreview = result;
    optId("qualification-variable-count").textContent = result.plan.variable_count;
    optId("qualification-corner-count").textContent = result.plan.corner_count;
    optId("qualification-point-count").textContent = result.plan.point_count;
    optId("qualification-run-count").textContent = result.execution.total_run_count;
    optId("qualification-method").textContent = "Digit-scrambled Halton";
    optId("qualification-preview-id").textContent = `${result.qualification_id} · ${result.plan.statistical_plan_id}`;
    renderQualificationModel(result);
    optId("qualification-preview-card").hidden = false;
    optId("qualification-status").className = "status-pill valid"; optId("qualification-status").textContent = "Valid preview";
    qualificationErrors([]);
  } catch (error) {
    latestQualificationPreview = null; optId("qualification-preview-card").hidden = true;
    optId("qualification-status").className = "status-pill invalid"; optId("qualification-status").textContent = "Invalid";
    qualificationErrors([error.message]);
  } finally { button.disabled = false; button.textContent = "Preview qualification"; }
}

async function freezeQualification() {
  if (!latestQualificationPreview || !selectedQualificationSource) return;
  const button = optId("qualification-freeze"); button.disabled = true; button.textContent = "Publishing…";
  try {
    const request = qualificationRequest();
    const response = await fetch("/api/qualification/freeze", {method: "POST", headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"}, body: JSON.stringify({
      ...request, expected_qualification_id: latestQualificationPreview.qualification_id,
      expected_statistical_plan_id: latestQualificationPreview.plan.statistical_plan_id,
      expected_total_run_count: latestQualificationPreview.execution.total_run_count,
    })});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Qualification publication failed");
    frozenQualificationLaunch = result;
    optId("qualification-plan-id").textContent = `${result.plan.plan_id} · ${result.plan.artifact}`;
    optId("qualification-acknowledgement").checked = false; optId("qualification-start").disabled = true;
    optId("qualification-confirmation").hidden = false; qualificationErrors([]);
  } catch (error) { qualificationErrors([error.message]); }
  finally { button.disabled = false; button.textContent = "Publish immutable qualification"; }
}

function renderQualificationJob(job) {
  trackedQualificationJob = job;
  const box = optId("qualification-job");
  const title = document.createElement("strong"); title.textContent = `${job.qualification_job_id} · ${optimizationLabel(job.status)}`;
  const progress = document.createElement("div"); progress.className = "optimization-progress";
  progress.append(optimizationProgressRow("Paired LTspice runs", job.progress.finished_points, job.progress.total_runs));
  for (const child of job.experiments) progress.append(optimizationProgressRow(`${child.name.toUpperCase()} · ${child.status}`, child.finished_points, child.point_count));
  const actions = document.createElement("div"); actions.className = "job-actions";
  if (["defined", "queued", "running", "cancelling"].includes(job.status)) {
    const cancel = document.createElement("button"); cancel.className = "secondary-button"; cancel.textContent = "Cancel remaining runs"; cancel.addEventListener("click", () => mutateQualificationJob("cancel")); actions.append(cancel);
  } else if (job.resumable) {
    const resume = document.createElement("button"); resume.className = "secondary-button"; resume.textContent = "Resume unfinished runs"; resume.addEventListener("click", () => mutateQualificationJob("resume")); actions.append(resume);
  }
  if (job.error) { const error = document.createElement("p"); error.className = "job-error"; error.textContent = job.error; actions.append(error); }
  box.replaceChildren(title, progress, actions); box.hidden = false;
  if (job.results_url) loadQualificationResults(job);
  window.clearTimeout(qualificationPollTimer);
  if (["defined", "queued", "running", "cancelling"].includes(job.status)) qualificationPollTimer = window.setTimeout(pollQualificationJob, 750);
}

async function startQualification() {
  if (!frozenQualificationLaunch || !optId("qualification-acknowledgement").checked) return;
  const button = optId("qualification-start"); button.disabled = true; button.textContent = "Queuing…";
  try {
    const response = await fetch("/api/qualification/start", {method: "POST", headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"}, body: JSON.stringify({launch_token: frozenQualificationLaunch.launch_token, confirmed_total_run_count: frozenQualificationLaunch.execution.total_run_count, acknowledged: true})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || "Qualification launch failed");
    button.textContent = "Qualification queued"; renderQualificationJob(result);
  } catch (error) { qualificationErrors([error.message]); button.disabled = false; button.textContent = "Start local qualification"; }
}

async function pollQualificationJob() {
  if (!trackedQualificationJob) return;
  try { const response = await fetch(`/api/qualification/jobs/${trackedQualificationJob.qualification_job_id}`); const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || "Qualification status is unavailable"); renderQualificationJob(result); }
  catch (error) { qualificationErrors([error.message]); }
}

async function mutateQualificationJob(action) {
  if (!trackedQualificationJob) return;
  try { const response = await fetch(`/api/qualification/jobs/${trackedQualificationJob.qualification_job_id}/${action}`, {method: "POST", headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"}, body: "{}"}); const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || `Qualification ${action} failed`); renderQualificationJob(result); }
  catch (error) { qualificationErrors([error.message]); }
}

function qualificationRows(containerId, records, render) {
  const rows = records.map((record) => { const row = document.createElement("div"); const values = render(record); for (const value of values) { const cell = document.createElement("span"); cell.textContent = value; row.append(cell); } return row; });
  optId(containerId).replaceChildren(...rows);
}

async function loadQualificationResults(job) {
  if (!job.results_url || displayedQualificationStudy === job.qualification_study_id) return;
  try {
    const response = await fetch(job.results_url); const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || "Qualification results unavailable");
    displayedQualificationStudy = result.study_id;
    const corners = result.corner_results || []; const evaluated = corners.reduce((sum, item) => sum + Number(item.evaluated), 0); const passed = corners.reduce((sum, item) => sum + Number(item.passed), 0);
    const metrics = [["Joint yield", evaluated ? `${(100 * passed / evaluated).toFixed(2)}%` : "n/a"], ["Worst corner", result.worst_corner_yield === null ? "n/a" : `${(100 * result.worst_corner_yield).toFixed(2)}%`], ["Evaluated", evaluated], ["Failed / invalid", `${evaluated - passed} / ${corners.reduce((sum, item) => sum + Number(item.invalid), 0)}`]].map(([labelText, valueText]) => { const item = document.createElement("div"); const label = document.createElement("span"); label.textContent = labelText; const value = document.createElement("strong"); value.textContent = valueText; item.append(label, value); return item; });
    optId("qualification-result-metrics").replaceChildren(...metrics);
    qualificationRows("qualification-corner-results", corners, (item) => [Object.entries(item.corners).map(([key, value]) => `${key}=${value}`).join(", "), `${item.passed}/${item.evaluated}`, item.observed_yield === null ? "n/a" : `${(100 * item.observed_yield).toFixed(2)}%`, `${(100 * item.confidence_low).toFixed(2)}–${(100 * item.confidence_high).toFixed(2)}%`]);
    qualificationRows("qualification-margin-results", (result.worst_requirements || []).slice().sort((a, b) => Number(a.margin) - Number(b.margin)), (item) => [`${item.experiment} / ${item.metric}`, `${optimizationEngineeringValue(item.value, item.unit)} ${item.operator} ${optimizationEngineeringValue(item.target, item.unit)}`, `${item.margin >= 0 ? "+" : ""}${optimizationEngineeringValue(item.margin, item.unit)}`]);
    qualificationRows("qualification-sensitivities", result.dominant_sensitivities || [], (item) => [`${item.experiment} / ${item.metric}`, String(item.variable), `ρ ${Number(item.rho).toFixed(3)}`]);
    qualificationRows("qualification-failures", result.failed_points || [], (item) => [`Sample ${item.sample_index}`, Object.entries(item.corners).map(([key, value]) => `${key}=${value}`).join(", "), optimizationLabel(item.classification)]);
    optId("qualification-failures-summary").textContent = `Failed samples (${(result.failed_points || []).length})`;
    const links = Object.entries(result.evidence || {}).map(([name, url]) => { const link = document.createElement("a"); link.href = url; link.target = "_blank"; link.rel = "noopener"; link.textContent = name === "report" ? "Full HTML report" : name.toUpperCase(); return link; });
    optId("qualification-evidence-links").replaceChildren(...links); optId("qualification-results").hidden = false;
  } catch (error) { qualificationErrors([error.message]); }
}

async function recoverQualificationJob() {
  // limit=8 (the server's default page size), not 1: this list is not
  // filtered by source study/candidate server-side, so recovery has to
  // search recent jobs client-side. limit=1 made that search a no-op
  // whenever the most recent qualification job belonged to a different
  // candidate than the one currently selected.
  const response = await fetch("/api/qualification/jobs?limit=8"); if (!response.ok) return;
  const result = await response.json();
  const job = result.jobs.find((item) => item.source_study_id === selectedQualificationSource?.study_id && Number(item.source_candidate_index) === Number(selectedQualificationSource?.candidate_index));
  if (job) renderQualificationJob(job);
}

async function loadOptimizationResults(job) {
  if (!job.results_url || displayedOptimizationStudy === job.optimization_study_id) return;
  try {
    const response = await fetch(job.results_url);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Optimization results are unavailable");
    renderOptimizationResults(result);
  } catch (error) {
    renderOptimizationErrors([{path: "optimization results", message: error.message}]);
  }
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
  if (job.results_url) loadOptimizationResults(job);
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
  displayedOptimizationStudy = null;
  optId("optimization-results").hidden = true;
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
optId("qualification-preview").addEventListener("click", previewQualification);
optId("qualification-freeze").addEventListener("click", freezeQualification);
optId("qualification-acknowledgement").addEventListener("change", () => {
  optId("qualification-start").disabled = !optId("qualification-acknowledgement").checked;
});
optId("qualification-start").addEventListener("click", startQualification);

loadOptimizationReference().catch((error) => renderOptimizationPreview({
  valid: false,
  errors: [{path: "optimization", message: error.message}],
  limits: {maximum_candidates: 512, maximum_points: 1000},
}));
