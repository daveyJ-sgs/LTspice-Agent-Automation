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
let optimizationDirty = false; // true once the loaded recipe has edits Save hasn't persisted yet
let currentOptimizationProjectSlug = null; // the open Optimization project's slug, if any
let currentOptimizationProjectPath = null; // workspace-relative folder of the open Optimization project, if any
let qualificationPollTimer = null;
let displayedQualificationStudy = null;

const optId = (id) => document.getElementById(id);

// See setCurrentStudyProject() in app.js -- Optimization tracks its own
// project association independently so opening a Study project can never
// mislabel this tab's Save button as attached to it.
function setCurrentOptimizationProject(slug, path) {
  currentOptimizationProjectSlug = slug;
  currentOptimizationProjectPath = path;
  optId("optimization-save").textContent = slug ? "Save to project" : "Save recipe";
}

function showOptimizationEmptyState() {
  optId("optimization-empty").hidden = false;
  optId("optimization-grid").hidden = true;
  optId("optimization-results").hidden = true;
  optId("optimization-save").hidden = true;
  optId("optimization-preview").hidden = true;
  optId("optimization-title").textContent = "No optimization recipe loaded";
  optId("optimization-description").textContent =
    "Open an optimization project from the Projects tab, or load a .ltopt.json file to get started.";
}

const OPT_UNITS = {
  F: [["pF", "pF", 1e-12], ["nF", "nF", 1e-9], ["uF", "µF", 1e-6]],
  ohm: [["ohm", "Ω", 1], ["kohm", "kΩ", 1e3], ["Mohm", "MΩ", 1e6]],
};

function optNumber(value) {
  if (String(value).trim() === "") return "";
  const parsed = Number(value);
  if (Number.isFinite(parsed)) return parsed;
  // SPICE suffixes, as in the netlist (spiceNumber lives in app.js).
  const spice = spiceNumber(value);
  return Number.isFinite(spice) ? spice : value;
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

// Every list in the recipe -- domains, corner axes, objectives, constraints --
// can grow and shrink here, so a recipe can be built in the editor rather
// than only tuned.
function removeListItem(list, index, label, render) {
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-button";
  remove.textContent = "\u00d7";
  remove.title = `Remove ${label}`;
  remove.setAttribute("aria-label", `Remove ${label}`);
  remove.addEventListener("click", () => {
    list.splice(index, 1);
    render();
    scheduleOptimizationPreview();
  });
  return remove;
}

function addListButton(text, key, make, render) {
  const add = document.createElement("button");
  add.type = "button";
  add.className = "compact-button";
  add.textContent = text;
  add.addEventListener("click", () => {
    const list = optimizationRecipe[key] || (optimizationRecipe[key] = []);
    list.push(make(list.length + 1));
    render();
    scheduleOptimizationPreview();
  });
  return add;
}

// A new objective or constraint starts on the study and analysis the recipe
// already measures, which is nearly always where the next one belongs.
function selectorDefaults() {
  const known = [...(optimizationRecipe.objectives || []), ...(optimizationRecipe.constraints || [])][0];
  return {
    experiment: known?.experiment || "ac",
    analysis: known?.analysis || "",
    metric: known?.metric || "ac_gain_db",
    metric_parameters: {...(known?.metric_parameters || {})},
  };
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
    const remove = removeListItem(parameters, index, `domain ${parameter.name}`, renderOptimizationDomains);
    for (const control of [name, kind, domain, unit, remove]) {
      const cell = document.createElement("td");
      cell.append(control);
      row.append(cell);
    }
    return row;
  });
  optId("optimization-domains").replaceChildren(...rows);
  optId("optimization-domain-add").replaceChildren(addListButton("+ Domain", "parameters", (number) => ({
    name: `PARAM${number}`, kind: "continuous", minimum: 1, maximum: 2, count: 2, unit: "",
  }), renderOptimizationDomains));
  optId("optimization-fixed").replaceChildren(
    fixedParameterEditor(optimizationRecipe, renderOptimizationDomains, "No fixed conditions."),
  );
}

// Held-constant circuit conditions, as name/value pairs. Shared by the
// optimization recipe and the qualification model, which carry the same shape.
function fixedParameterEditor(owner, render, emptyText) {
  const wrap = document.createElement("div");
  wrap.className = "fixed-parameters";
  const entries = Object.entries(owner.fixed_parameters || {});
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "editor-empty";
    empty.textContent = emptyText;
    wrap.append(empty);
  }
  for (const [name, value] of entries) {
    const row = document.createElement("div");
    row.className = "fixed-parameter-row";
    const key = optInput(name, `fixed parameter name ${name}`, () => {});
    key.addEventListener("change", () => {
      const renamed = key.value.trim();
      const current = owner.fixed_parameters;
      if (!renamed || renamed === name) { key.value = name; return; }
      if (renamed in current) { key.value = name; return; }
      // Rebuild in place so the pair keeps its position in the list.
      const rebuilt = {};
      for (const [existing, held] of Object.entries(current)) {
        rebuilt[existing === name ? renamed : existing] = held;
      }
      owner.fixed_parameters = rebuilt;
      render();
      scheduleOptimizationPreview();
    });
    const held = optInput(value, `fixed parameter value ${name}`, (entered) => {
      owner.fixed_parameters[name] = optNumber(String(entered).trim());
      scheduleOptimizationPreview();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-button";
    remove.textContent = "\u00d7";
    remove.title = `Remove ${name}`;
    remove.setAttribute("aria-label", `Remove fixed parameter ${name}`);
    remove.addEventListener("click", () => {
      delete owner.fixed_parameters[name];
      if (Object.keys(owner.fixed_parameters).length === 0) delete owner.fixed_parameters;
      render();
      scheduleOptimizationPreview();
    });
    row.append(key, held, remove);
    wrap.append(row);
  }
  const add = document.createElement("button");
  add.type = "button";
  add.className = "compact-button";
  add.textContent = "+ Fixed condition";
  add.addEventListener("click", () => {
    const held = owner.fixed_parameters || (owner.fixed_parameters = {});
    let suffix = Object.keys(held).length + 1;
    while (`PARAM${suffix}` in held) suffix += 1;
    held[`PARAM${suffix}`] = 0;
    render();
    scheduleOptimizationPreview();
  });
  wrap.append(add);
  return wrap;
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
    const heading = document.createElement("div");
    heading.className = "editor-card-actions";
    heading.append(removeListItem(optimizationRecipe.corner_axes, axisIndex, `corner axis ${axis.name}`, renderOptimizationCorners));
    card.append(heading, fields, values);
    return card;
  });
  optId("optimization-corners").replaceChildren(...cards, addListButton("+ Corner axis", "corner_axes", (number) => ({
    name: `corner_${number}`, parameter: "", unit: "", values: [{name: "low", value: 1}, {name: "high", value: 2}],
  }), renderOptimizationCorners));
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

// The .ltopt goal schema nests its parameters under metric_parameters, where a
// .ltstudy requirement carries them as flat sibling keys. The two shapes are
// not interchangeable, so this editor keeps the nested form and only borrows
// the metric list and the per-metric parameter names from the shared schema.
function optMetricSelect(item, caption) {
  const select = document.createElement("select");
  select.setAttribute("aria-label", `${item.name} ${caption}`);
  let matched = false;
  for (const [label, names] of metricOptionGroups()) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const name of names) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      if (name === item.metric) matched = true;
      group.append(option);
    }
    select.append(group);
  }
  if (!matched) {
    const option = document.createElement("option");
    option.value = item.metric ?? "";
    option.textContent = item.metric ? `${item.metric} (loaded)` : "\u2014 Select a metric \u2014";
    select.prepend(option);
  }
  select.value = item.metric ?? "";
  return select;
}

function metricArgumentProblem(item) {
  // Say nothing about a metric the schema does not describe, rather than
  // reporting every argument of it as unknown.
  const definition = metricDefinition(item.metric);
  if (!definition) return "";
  const accepted = definition.parameters;
  const names = new Set(accepted.map((parameter) => parameter.name));
  const supplied = Object.keys(item.metric_parameters || {});
  const unknown = supplied.filter((name) => !names.has(name));
  if (unknown.length) {
    return `${item.metric} has no ${unknown[0]}; it takes ${[...names].join(", ") || "no arguments"}.`;
  }
  const missing = accepted
    .filter((parameter) => parameter.required && !supplied.includes(parameter.name))
    .map((parameter) => parameter.name);
  // A goal selects an already-measured result, so a required parameter that is
  // left out matches every value of it -- ambiguous as soon as the study sweeps
  // more than one.
  return missing.length ? `Add ${missing[0]} to pick one ${item.metric} result.` : "";
}

function metricArgumentPlaceholder(item) {
  const accepted = metricParameters(item.metric).filter((parameter) => !parameter.common);
  return accepted.length ? accepted.map((parameter) => `${parameter.name}=`).join(", ") : "none";
}

function metricField(item, render) {
  const select = optMetricSelect(item, "Metric");
  select.addEventListener("change", () => {
    const definition = metricDefinition(select.value);
    if (definition) {
      const names = new Set(definition.parameters.map((parameter) => parameter.name));
      for (const key of Object.keys(item.metric_parameters || {})) {
        if (!names.has(key)) delete item.metric_parameters[key];
      }
      if (item.metric_parameters && Object.keys(item.metric_parameters).length === 0) {
        delete item.metric_parameters;
      }
    }
    item.metric = select.value;
    render();
    scheduleOptimizationPreview();
  });
  return optField("Metric", select);
}

function metricArgumentsField(item, render) {
  const problem = document.createElement("span");
  problem.className = "field-problem";
  const input = optInput(metricParametersText(item), `${item.name} metric arguments`, (value) => {
    setMetricParameters(item, value);
    refresh();
    scheduleOptimizationPreview();
  });
  input.placeholder = metricArgumentPlaceholder(item);

  function refresh() {
    const message = metricArgumentProblem(item);
    problem.textContent = message;
    problem.hidden = !message;
    input.setAttribute("aria-invalid", message ? "true" : "false");
  }
  refresh();

  const field = optField("Metric arguments", input);
  field.append(problem);
  return field;
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

// The engine reads absolute_tolerance and relative_tolerance as a pair: if
// either key is present both are read, neither may be negative, and they may
// not both be zero. So the editor writes both or neither.
function toleranceField(item, key, caption, render) {
  const problem = document.createElement("span");
  problem.className = "field-problem";
  const input = optInput(item[key] ?? "", `${item.name} ${caption}`, (value) => {
    const entered = String(value).trim();
    if (entered === "") delete item[key];
    else item[key] = optNumber(entered);
    const other = key === "absolute_tolerance" ? "relative_tolerance" : "absolute_tolerance";
    // Supplying one alone means the other defaults to zero, which is valid
    // only while this one is above zero; pin it so the pair is always whole.
    if (item[key] !== undefined && item[other] === undefined) item[other] = 0;
    if (item[key] === undefined && item[other] === 0) delete item[other];
    refresh();
    render();
    scheduleOptimizationPreview();
  });
  input.placeholder = "0";

  function refresh() {
    problem.textContent = toleranceProblem(item);
    problem.hidden = !problem.textContent;
    input.setAttribute("aria-invalid", problem.textContent ? "true" : "false");
  }
  refresh();

  const field = optField(caption, input);
  field.append(problem);
  return field;
}

function toleranceProblem(item) {
  const absolute = item.absolute_tolerance;
  const relative = item.relative_tolerance;
  if (absolute === undefined && relative === undefined) return "";
  const values = [absolute ?? 0, relative ?? 0].map(Number);
  if (values.some((value) => !Number.isFinite(value))) return "Tolerances must be numbers.";
  if (values.some((value) => value < 0)) return "Tolerances cannot be negative.";
  if (values.every((value) => value === 0)) return "One tolerance must be above zero.";
  return "";
}

function renderOptimizationSelectors() {
  const objectives = optimizationRecipe.objectives || [];
  optId("optimization-objective-count").textContent = `${objectives.length} objectives`;
  optId("optimization-objectives").replaceChildren(...objectives.map((item, index) => {
    const row = document.createElement("div");
    row.className = "optimization-row";
    row.append(removeListItem(objectives, index, `objective ${item.name}`, renderOptimizationSelectors));
    row.append(
      selectorField(item, "name", "Name"),
      selectorField(item, "experiment", "Study", [["ac", "AC"], ["transient", "Transient"]]),
      selectorField(item, "analysis", "Analysis"),
      metricField(item, renderOptimizationSelectors),
      selectorField(item, "goal", "Goal", [["minimize", "Minimize"], ["maximize", "Maximize"]]),
      selectorField(item, "weight", "Weight", null, true),
      metricArgumentsField(item, renderOptimizationSelectors),
      toleranceField(item, "absolute_tolerance", "Abs. tolerance", renderOptimizationSelectors),
      toleranceField(item, "relative_tolerance", "Rel. tolerance", renderOptimizationSelectors),
    );
    return row;
  }), addListButton("+ Objective", "objectives", (number) => ({
    name: `objective_${number}`, ...selectorDefaults(), goal: "minimize", weight: 1,
    absolute_tolerance: 0, relative_tolerance: 0,
  }), renderOptimizationSelectors));

  const constraints = optimizationRecipe.constraints || [];
  optId("optimization-constraint-count").textContent = `${constraints.length} constraints`;
  optId("optimization-constraints").replaceChildren(...constraints.map((item, index) => {
    const row = document.createElement("div");
    row.className = "optimization-row constraint";
    row.append(removeListItem(constraints, index, `constraint ${item.name}`, renderOptimizationSelectors));
    row.append(
      selectorField(item, "name", "Name"),
      selectorField(item, "experiment", "Study", [["ac", "AC"], ["transient", "Transient"]]),
      selectorField(item, "analysis", "Analysis"),
      metricField(item, renderOptimizationSelectors),
      selectorField(item, "operator", "Limit", [["<", "<"], ["<=", "≤"], [">", ">"], [">=", "≥"]]),
      selectorField(item, "target", "Target", null, true),
      metricArgumentsField(item, renderOptimizationSelectors),
    );
    return row;
  }), addListButton("+ Constraint", "constraints", (number) => ({
    name: `constraint_${number}`, ...selectorDefaults(), operator: "<=", target: 0,
  }), renderOptimizationSelectors));
}

function renderOptimizationEditors() {
  markClean("optimization-save-status", (v) => { optimizationDirty = v; });
  optId("optimization-empty").hidden = true;
  optId("optimization-grid").hidden = false;
  optId("optimization-save").hidden = false;
  optId("optimization-preview").hidden = false;
  optId("optimization-title").textContent = optimizationRecipe.title || "Untitled optimization";
  optId("optimization-description").textContent =
    optimizationRecipe.description || "Portable LTspice optimization recipe";
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
    icon.textContent = name.slice(0, 2).toUpperCase();
    const detail = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = name;
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
  markDirty("optimization-save-status", (v) => { optimizationDirty = v; });
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
  optId("refine-optimization-link").hidden = !available;
  optId("robust-selection-link").hidden = !available;
  optId("qualification-panel").hidden = !available;
  if (available) renderQualificationModelEditor();
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
  renderRobustFinalists(result);
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

// The manufacturing tolerance model the qualification samples over. It lives
// in the .ltopt's qualification block and was previously visible only as the
// resolved read-only summary a preview returned.
const QUALIFICATION_VARIABLE_KEYS = ["sigma_fraction", "minimum_factor", "maximum_factor"];

function renderQualificationModelEditor() {
  const model = optimizationRecipe?.qualification;
  const editor = optId("qualification-model-editor");
  if (!model) { editor.hidden = true; return; }
  editor.hidden = false;
  const variables = model.variables || (model.variables = []);
  const rows = variables.map((variable, index) => {
    const row = document.createElement("div");
    row.className = "qualification-variable-row";
    const name = optInput(variable.name, `qualification variable ${index + 1} name`, (value) => {
      variable.name = String(value).trim();
      scheduleOptimizationPreview();
    });
    row.append(optField("Parameter", name));

    // Components are specified by a +/- tolerance band, so that is the control:
    // entering it fills the limit factors, and sigma at one third of the band
    // so the +/-3 sigma spread matches the part's own rating.
    const tolerance = optInput(
      tolerancePercent(variable),
      `qualification ${variable.name} tolerance percent`,
      (value) => {
        const entered = String(value).trim();
        if (entered === "") return;
        const percent = Number(entered);
        if (!Number.isFinite(percent) || percent < 0) return;
        applyTolerancePercent(variable, percent);
        renderQualificationModelEditor();
        scheduleOptimizationPreview();
      },
    );
    tolerance.placeholder = "5";
    row.append(optField("Tolerance \u00b1%", tolerance));

    for (const [key, caption] of [
      ["minimum_factor", "Min factor"],
      ["maximum_factor", "Max factor"],
      ["sigma_fraction", "Sigma fraction"],
    ]) {
      const input = optInput(variable[key], `qualification ${variable.name} ${key}`, (value) => {
        variable[key] = optNumber(String(value).trim());
        // A hand-edited limit can make the band asymmetric, which no single
        // tolerance percentage describes -- the field reads "custom" then.
        tolerance.value = tolerancePercent(variable);
        scheduleOptimizationPreview();
      });
      row.append(optField(caption, input));
    }

    const unit = optInput(variable.unit ?? "", `qualification ${variable.name} unit`, (value) => {
      // The engine requires exactly these five keys, so unit is always
      // written even when it is blank.
      variable.unit = String(value);
      scheduleOptimizationPreview();
    });
    row.append(optField("Unit", unit));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-button";
    remove.textContent = "\u00d7";
    remove.title = `Remove ${variable.name}`;
    remove.setAttribute("aria-label", `Remove qualification variable ${variable.name}`);
    remove.addEventListener("click", () => {
      variables.splice(index, 1);
      renderQualificationModelEditor();
      scheduleOptimizationPreview();
    });
    row.append(remove);
    return row;
  });
  const add = document.createElement("button");
  add.type = "button";
  add.className = "compact-button";
  add.textContent = "+ Toleranced parameter";
  add.addEventListener("click", () => {
    const variable = {name: "", sigma_fraction: 0, minimum_factor: 0, maximum_factor: 0, unit: ""};
    applyTolerancePercent(variable, 5);
    variables.push(variable);
    renderQualificationModelEditor();
    scheduleOptimizationPreview();
  });
  optId("qualification-variables").replaceChildren(...rows, add);
  optId("qualification-fixed").replaceChildren(
    fixedParameterEditor(model, renderQualificationModelEditor, "No fixed conditions."),
  );
}

// A +/-t% band becomes limit factors 1-t and 1+t, with sigma at t/3 so the
// part's rating sits at three sigma.
function applyTolerancePercent(variable, percent) {
  const fraction = percent / 100;
  variable.minimum_factor = round12(1 - fraction);
  variable.maximum_factor = round12(1 + fraction);
  variable.sigma_fraction = round12(fraction / 3);
}

function tolerancePercent(variable) {
  const below = 1 - Number(variable.minimum_factor);
  const above = Number(variable.maximum_factor) - 1;
  if (!Number.isFinite(below) || !Number.isFinite(above)) return "";
  if (Math.abs(below - above) > 1e-9) return "custom";
  return String(round12(above * 100));
}

// Keeps 1 - 0.05 from serialising as 0.9500000000000001.
function round12(value) {
  return Number(Number(value).toPrecision(12));
}

// Only feasible selected or Pareto candidates can be finalists -- the engine
// refuses anything else, so the picker offers only those.
function renderRobustFinalists(result) {
  const eligible = (result.candidates || []).filter(
    (candidate) => candidate.status === "feasible" && (candidate.selected || candidate.pareto),
  );
  const host = optId("robust-finalists");
  if (eligible.length < 2) {
    const note = document.createElement("p");
    note.className = "muted-copy";
    note.textContent = "This study has only one feasible Pareto candidate, so there is nothing to compare it against.";
    host.replaceChildren(note);
    optId("robust-run").disabled = true;
    return;
  }
  optId("robust-run").disabled = false;
  host.replaceChildren(...eligible.map((candidate) => {
    const label = document.createElement("label");
    label.className = "trace-toggle";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.id = `finalist-${candidate.candidate_index}`;
    box.value = String(candidate.candidate_index);
    box.checked = Boolean(candidate.selected);
    const text = document.createElement("span");
    text.textContent = `candidate ${candidate.candidate_index}${candidate.selected ? " (winner)" : ""}`;
    label.append(box, text);
    return label;
  }));
}

async function runRobustSelection() {
  if (!displayedOptimizationStudy) return;
  const chosen = [...document.querySelectorAll("#robust-finalists input:checked")]
    .map((box) => Number(box.value));
  const status = optId("robust-status");
  if (chosen.length < 2) {
    status.textContent = "Pick at least two candidates to compare.";
    return;
  }
  const button = optId("robust-run");
  button.disabled = true;
  status.textContent = "Freezing paired tolerance plans\u2026";
  try {
    const response = await fetch("/api/optimization/robust-selection", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        study_id: displayedOptimizationStudy,
        finalists: chosen,
        sample_count: Number(optId("robust-samples").value),
        seed: Number(optId("robust-seed").value),
        qualification: optimizationRecipe?.qualification,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Selection plan failed");
    status.textContent =
      `Plan ${result.selection_id || result.plan_id} froze ${chosen.length} finalists`
      + `${result.point_count ? ` \u00b7 ${result.point_count} points each` : ""}`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

optId("robust-run").addEventListener("click", runRobustSelection);

async function refineOptimization() {
  if (!displayedOptimizationStudy) return;
  const button = optId("refine-optimization");
  const status = optId("refine-status");
  button.disabled = true;
  status.textContent = "Freezing refined candidates\u2026";
  try {
    const response = await fetch("/api/optimization/refine", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        parent_study_id: displayedOptimizationStudy,
        max_candidates: Number(optId("refine-candidates").value),
        max_points: Number(optId("refine-points").value),
        recipe: optimizationRecipe,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Refinement failed");
    const refinement = result.refinement || {};
    status.textContent =
      `Refined plan ${refinement.plan_id} is running \u00b7 `
      + `${refinement.candidate_count} candidates \u00b7 ${refinement.point_count} points`;
    renderOptimizationJob(result);
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

optId("refine-optimization").addEventListener("click", refineOptimization);

function qualificationRequest() {
  return {
    ...selectedQualificationSource,
    execution: optimizationRecipe?.execution,
    qualification: optimizationRecipe?.qualification,
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
  // The status pill otherwise only gets set by previewQualification(), so a
  // job recovered from a page reload (or loaded via the Dashboard/Qualify
  // link) left it stuck on its default "Not previewed" text even though a
  // completed run's results were already showing right below it.
  const statusPill = optId("qualification-status");
  if (job.status === "completed") {
    statusPill.className = "status-pill valid";
    statusPill.textContent = "Completed";
  } else if (job.status === "failed") {
    statusPill.className = "status-pill invalid";
    statusPill.textContent = "Failed";
  } else {
    statusPill.className = "status-pill idle";
    statusPill.textContent = optimizationLabel(job.status);
  }
  const box = optId("qualification-job");
  const title = document.createElement("strong"); title.textContent = `${job.qualification_job_id} · ${optimizationLabel(job.status)}`;
  const progress = document.createElement("div"); progress.className = "optimization-progress";
  progress.append(optimizationProgressRow("Paired LTspice runs", job.progress.finished_points, job.progress.total_runs));
  for (const child of job.experiments) progress.append(optimizationProgressRow(`${child.name.toUpperCase()} · ${child.status}`, child.finished_points, child.point_count));
  const actions = document.createElement("div"); actions.className = "job-actions";
  if (["defined", "queued", "running", "cancelling"].includes(job.status)) {
    actions.append(parentJobActionButton("Cancel remaining runs", qualificationPoll, () => mutateQualificationJob("cancel")));
  } else if (job.resumable) {
    actions.append(parentJobActionButton("Resume unfinished runs", qualificationPoll, () => mutateQualificationJob("resume")));
  }
  const actionError = actionErrorNote(qualificationPoll);
  if (actionError) actions.append(actionError);
  if (job.error) { const error = document.createElement("p"); error.className = "job-error"; error.textContent = job.error; actions.append(error); }
  const problem = pollProblemRow(qualificationPoll, pollQualificationJob);
  box.replaceChildren(title, ...(problem ? [problem] : []), progress, actions); box.hidden = false;
  if (job.results_url) loadQualificationResults(job);
  window.clearTimeout(qualificationPollTimer);
  if (["defined", "queued", "running", "cancelling"].includes(job.status) && qualificationPoll.failures < POLL_MAX_FAILURES) {
    qualificationPollTimer = window.setTimeout(pollQualificationJob, pollDelay(qualificationPoll, 750));
  }
}

async function startQualification() {
  if (!frozenQualificationLaunch || !optId("qualification-acknowledgement").checked) return;
  const button = optId("qualification-start"); button.disabled = true; button.textContent = "Queuing…";
  try {
    const response = await fetch("/api/qualification/start", {method: "POST", headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"}, body: JSON.stringify({launch_token: frozenQualificationLaunch.launch_token, confirmed_total_run_count: frozenQualificationLaunch.execution.total_run_count, acknowledged: true})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || "Qualification launch failed");
    button.textContent = "Qualification queued"; renderQualificationJob(result);
  } catch (error) { qualificationErrors([error.message]); syncStartButton("qualification-start"); button.textContent = "Start local qualification"; }
}

async function pollQualificationJob() {
  if (!trackedQualificationJob) return;
  const polled = trackedQualificationJob.qualification_job_id;
  try {
    const response = await fetch(`/api/qualification/jobs/${encodeURIComponent(polled)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Qualification status is unavailable");
    if (trackedQualificationJob?.qualification_job_id !== polled) return;
    qualificationPoll.failures = 0;
    qualificationPoll.error = "";
    renderQualificationJob(result);
  } catch (error) {
    if (trackedQualificationJob?.qualification_job_id !== polled) return;
    qualificationPoll.failures += 1;
    qualificationPoll.error = error.message;
    renderQualificationJob(trackedQualificationJob);
  }
}

async function mutateQualificationJob(action) {
  if (!trackedQualificationJob) return;
  const response = await fetch(`/api/qualification/jobs/${encodeURIComponent(trackedQualificationJob.qualification_job_id)}/${action}`, {method: "POST", headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"}, body: "{}"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || `Qualification ${action} failed`);
  qualificationPoll.failures = 0;
  qualificationPoll.error = "";
  renderQualificationJob(result);
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
    qualificationRows("qualification-corner-results", corners, (item) => [Object.entries(item.corners).map(([key, value]) => `${key}=${value}`).join(", "), `${item.passed}/${item.evaluated}`, item.observed_yield === null ? "n/a" : `${(100 * item.observed_yield).toFixed(2)}%`, item.confidence_low === null || item.confidence_high === null ? (item.confidence_reason || "Not available") : `${(100 * item.confidence_low).toFixed(2)}–${(100 * item.confidence_high).toFixed(2)}%`]);
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

// Shared by the optimization and qualification job polls: a failed status
// read backs off (base, 2x, 4x ... capped at 30s) and after
// POLL_MAX_FAILURES in a row stops, leaving an inline "Status unavailable --
// Retry" rather than dying silently on the first network blip.
const POLL_MAX_FAILURES = 6;
const optimizationPoll = {failures: 0, error: "", actionError: ""};
const qualificationPoll = {failures: 0, error: "", actionError: ""};

function pollDelay(poll, base) {
  return poll.failures ? Math.min(30000, base * 2 ** poll.failures) : base;
}

function pollProblemRow(poll, retry) {
  if (!poll.error) return null;
  const row = document.createElement("div");
  row.className = "poll-problem";
  row.setAttribute("role", "alert");
  const text = document.createElement("span");
  text.textContent = poll.failures >= POLL_MAX_FAILURES
    ? `Status unavailable: ${poll.error}`
    : `Status unavailable, retrying: ${poll.error}`;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "compact-button";
  button.textContent = "Retry";
  button.addEventListener("click", () => {
    poll.failures = 0;
    retry();
  });
  row.append(text, button);
  return row;
}

// Cancel/Resume for a parent job: disabled while the request is in flight,
// and a failure is shown beside the button rather than in the plan preview.
function parentJobActionButton(label, poll, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary-button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    poll.actionError = "";
    button.parentElement?.querySelectorAll(".job-error[role=alert]").forEach((node) => node.remove());
    try {
      await action();
    } catch (error) {
      poll.actionError = `${label} failed: ${error.message}`;
      const note = document.createElement("p");
      note.className = "job-error";
      note.setAttribute("role", "alert");
      note.textContent = poll.actionError;
      button.after(note);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });
  return button;
}

function actionErrorNote(poll) {
  if (!poll.actionError) return null;
  const note = document.createElement("p");
  note.className = "job-error";
  note.setAttribute("role", "alert");
  note.textContent = poll.actionError;
  return note;
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
    ? "Electrical analysis is complete. See the Pareto tradeoffs and winning candidate below."
    : `Optimization evaluation: ${job.progress.evaluation}.`;
  const errored = Number(job.progress.error_points || 0);
  const actions = document.createElement("div");
  actions.className = "job-actions";
  if (["defined", "queued", "running", "cancelling"].includes(job.status)) {
    actions.append(parentJobActionButton("Cancel remaining runs", optimizationPoll, () => mutateOptimizationJob("cancel")));
  } else if (job.resumable) {
    actions.append(parentJobActionButton("Resume unfinished runs", optimizationPoll, () => mutateOptimizationJob("resume")));
  }
  const actionError = actionErrorNote(optimizationPoll);
  if (actionError) actions.append(actionError);
  if (errored) {
    const note = document.createElement("p");
    note.className = "job-error";
    note.textContent = `${errored} LTspice run${errored === 1 ? "" : "s"} did not simulate.`;
    actions.append(note);
  }
  if (job.error) {
    const error = document.createElement("p");
    error.className = "job-error";
    error.textContent = job.error;
    actions.append(error);
  }
  const problem = pollProblemRow(optimizationPoll, pollOptimizationJob);
  container.replaceChildren(heading, ...(problem ? [problem] : []), structure, progress, evaluation, actions);
  container.hidden = false;
  if (job.results_url) loadOptimizationResults(job);
  window.clearTimeout(optimizationPollTimer);
  if (["defined", "queued", "running", "cancelling"].includes(job.status) && optimizationPoll.failures < POLL_MAX_FAILURES) {
    optimizationPollTimer = window.setTimeout(pollOptimizationJob, pollDelay(optimizationPoll, 750));
  }
}

async function pollOptimizationJob() {
  if (!trackedOptimizationJob) return;
  const polled = trackedOptimizationJob.optimization_job_id;
  try {
    const response = await fetch(`/api/optimization/jobs/${encodeURIComponent(polled)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Optimization status is unavailable");
    if (trackedOptimizationJob?.optimization_job_id !== polled) return;
    optimizationPoll.failures = 0;
    optimizationPoll.error = "";
    renderOptimizationJob(result);
  } catch (error) {
    if (trackedOptimizationJob?.optimization_job_id !== polled) return;
    optimizationPoll.failures += 1;
    optimizationPoll.error = error.message;
    renderOptimizationJob(trackedOptimizationJob);
  }
}

async function mutateOptimizationJob(action) {
  if (!trackedOptimizationJob) return;
  const response = await fetch(`/api/optimization/jobs/${encodeURIComponent(trackedOptimizationJob.optimization_job_id)}/${action}`, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-LTspice-System-Builder": "1"},
    body: "{}",
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || `Optimization ${action} failed`);
  optimizationPoll.failures = 0;
  optimizationPoll.error = "";
  renderOptimizationJob(result);
}

// Forget the job card when a different optimization recipe is opened, so one
// project's run never shows (or keeps polling) under another's editor.
function clearOptimizationJob() {
  window.clearTimeout(optimizationPollTimer);
  trackedOptimizationJob = null;
  optimizationPoll.failures = 0;
  optimizationPoll.error = "";
  optimizationPoll.actionError = "";
  optId("optimization-job").hidden = true;
  optId("optimization-job").replaceChildren();
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
    optimizationPoll.failures = 0;
    optimizationPoll.error = "";
    renderOptimizationJob(result);
  } catch (error) {
    renderOptimizationErrors([{path: "execution", message: error.message}]);
    syncStartButton("optimization-start");
    button.textContent = "Start local optimization";
  }
}

// Re-attaches the most recent job for the recipe that is open now, matched by
// the plan it resolves to -- not simply the newest job in the workspace,
// which may belong to another project entirely. Called after a recipe is
// opened or loaded and previewed, so a reload mid-run finds its job again.
async function recoverOptimizationJob() {
  const planId = latestOptimizationPreview?.plan?.plan_id;
  if (!planId || trackedOptimizationJob) return;
  const response = await fetch("/api/optimization/jobs?limit=32");
  if (!response.ok) return;
  const result = await response.json();
  if (latestOptimizationPreview?.plan?.plan_id !== planId || trackedOptimizationJob) return;
  const job = (result.jobs || []).find((item) => item.plan_id === planId);
  if (job) renderOptimizationJob(job);
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

optId("optimization-preview").addEventListener("click", previewOptimization);
optId("optimization-freeze").addEventListener("click", freezeOptimizationPlan);
optId("optimization-acknowledgement").addEventListener("change", () => {
  syncStartButton("optimization-start");
});
optId("optimization-start").addEventListener("click", startOptimization);
optId("optimization-file").addEventListener("change", async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  if (!confirmDiscard(optimizationDirty, "Loading a different recipe will discard unsaved changes to this one. Continue?")) {
    event.target.value = "";
    return;
  }
  try {
    optimizationRecipe = JSON.parse(await file.text());
    setCurrentOptimizationProject(null, null);
    optId("optimization-save-status").textContent = "";
    optimizationDisplayUnits = new WeakMap();
    displayedOptimizationStudy = null;
    clearOptimizationJob();
    optId("optimization-results").hidden = true;
    renderOptimizationEditors();
    await previewOptimization();
    recoverOptimizationJob().catch(() => {});
  } catch (error) {
    renderOptimizationPreview({
      valid: false,
      errors: [{path: "optimization", message: `Could not load recipe: ${error.message}`}],
      limits: {maximum_candidates: 512, maximum_points: 1000},
    });
  }
  event.target.value = "";
});
optId("optimization-save").addEventListener("click", async () => {
  if (!optimizationRecipe) return;
  const status = optId("optimization-save-status");
  if (!currentOptimizationProjectSlug) {
    const filenameSlug = (optimizationRecipe.title || "optimization")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "optimization";
    const blob = new Blob([`${JSON.stringify(optimizationRecipe, null, 2)}\n`], {type: "application/json"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${filenameSlug}.ltopt.json`;
    link.click();
    URL.revokeObjectURL(link.href);
    markClean("optimization-save-status", (v) => { optimizationDirty = v; });
    return;
  }
  const button = optId("optimization-save");
  button.disabled = true;
  status.classList.remove("is-error");
  status.textContent = "Saving…";
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(currentOptimizationProjectSlug)}/recipe`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify(optimizationRecipe),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Recipe could not be saved");
    optimizationDirty = false;
    status.classList.remove("unsaved");
    status.textContent = "Saved.";
    loadProjects();
  } catch (error) {
    status.classList.remove("unsaved");
    status.classList.add("is-error");
    status.textContent = `Not saved: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});
optId("qualification-preview").addEventListener("click", previewQualification);
optId("qualification-freeze").addEventListener("click", freezeQualification);
optId("qualification-acknowledgement").addEventListener("change", () => {
  syncStartButton("qualification-start");
});
optId("qualification-start").addEventListener("click", startQualification);

showOptimizationEmptyState();
