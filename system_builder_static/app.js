"use strict";

let recipe = null;
let previewTimer = null;
let previewSequence = 0;
let latestPreview = null;
let frozenLaunch = null;
let latestRemotePreview = null;
let remoteAuthReady = false;
let remoteJobs = new Map();
let trackedJobs = new Map();
let jobPollTimer = null;
let variableDisplayUnits = new WeakMap();
let cornerDisplayUnits = new WeakMap();
let netlistFiles = [];

const byId = (id) => document.getElementById(id);
const THEME_KEY = "ltspice-system-builder-theme";
const UNIT_CHOICES = {
  F: [["pF", "pF", 1e-12], ["nF", "nF", 1e-9], ["uF", "µF", 1e-6]],
  ohm: [["ohm", "Ω", 1], ["kohm", "kΩ", 1e3], ["Mohm", "MΩ", 1e6]],
};

function preferredTheme() {
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (["dark", "light"].includes(saved)) return saved;
  } catch (_) {
    // The system preference remains available if browser storage is disabled.
  }
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme(theme) {
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  byId("theme-toggle").setAttribute("aria-pressed", String(light));
  byId("theme-toggle").setAttribute(
    "aria-label",
    `Switch to ${light ? "dark" : "light"} mode`,
  );
  byId("theme-label").textContent = light ? "Dark" : "Light";
}

applyTheme(preferredTheme());

// Real view routing: exactly one top-level section is visible at a time,
// switched by clicking a nav link or a data-view control anywhere on the
// page, with the current view reflected in the URL hash so back/forward
// and reload land where you left off. This replaces the previous
// anchor-scroll navigation, where every section lived in the DOM at once
// and "switching" meant scrolling.
const VIEWS = ["dashboard", "projects", "definition", "optimization", "qualification", "history"];
const VIEW_LABELS = {
  dashboard: "Dashboard",
  projects: "Projects",
  definition: "Study setup",
  optimization: "Optimization",
  qualification: "Qualification",
  history: "Workspace",
};

function showView(view) {
  if (!VIEWS.includes(view)) view = "dashboard";
  for (const name of VIEWS) {
    const section = byId(name);
    if (section) section.hidden = name !== view;
  }
  document.querySelectorAll(".tool-nav [data-view]").forEach((link) => {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const crumb = byId("topbar-crumb");
  if (crumb) crumb.textContent = VIEW_LABELS[view];
  if (window.location.hash.slice(1) !== view) {
    window.history.replaceState(null, "", `#${view}`);
  }
  window.scrollTo({top: 0});
}

function routeFromHash() {
  showView((window.location.hash || "#dashboard").slice(1));
}

window.addEventListener("hashchange", routeFromHash);
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-view]");
  if (!trigger) return;
  event.preventDefault();
  showView(trigger.dataset.view);
});
routeFromHash();

function updateRecipeFromControls() {
  if (!recipe) return;
  recipe.plan.sample_count = numericValue(byId("sample-count").value);
  recipe.plan.seed = numericValue(byId("seed").value);
  recipe.plan.sampling_method = byId("sampling-method").value;
}

function numericValue(value) {
  if (value.trim() === "") return "";
  const number = Number(value);
  return Number.isFinite(number) ? number : value;
}

function defaultDisplayUnit(item) {
  const choices = UNIT_CHOICES[item.unit];
  if (!choices) return null;
  const magnitude = Math.abs(Number(item.nominal ?? item.values?.[0]?.value ?? 0));
  if (item.unit === "F") {
    if (magnitude >= 1e-6) return "uF";
    if (magnitude >= 1e-9) return "nF";
    return "pF";
  }
  if (magnitude >= 1e6) return "Mohm";
  if (magnitude >= 1e3) return "kohm";
  return "ohm";
}

function studyUnitFactor(canonicalUnit, displayUnit) {
  const choice = (UNIT_CHOICES[canonicalUnit] || []).find(([value]) => value === displayUnit);
  return choice ? choice[2] : 1;
}

function displayValue(baseValue, factor) {
  const number = Number(baseValue);
  if (!Number.isFinite(number)) return baseValue ?? "";
  return Number((number / factor).toPrecision(9)).toString();
}

function scaledFieldInput(baseValue, factor, path) {
  return fieldInput(displayValue(baseValue, factor), path);
}

function setScaledRecipeField(input, object, key, factor) {
  input.addEventListener("input", () => {
    const parsed = numericValue(input.value);
    object[key] = typeof parsed === "number"
      ? Number((parsed * factor).toPrecision(15))
      : parsed;
    schedulePreview();
  });
}

function unitSelect(item, displayUnits, path, onChange) {
  const choices = UNIT_CHOICES[item.unit];
  if (!choices) return null;
  const selected = displayUnits.get(item) || defaultDisplayUnit(item);
  displayUnits.set(item, selected);
  const select = selectInput(
    selected,
    choices.map(([value, label]) => [value, label]),
    path,
    "unit-selector",
  );
  select.addEventListener("change", () => {
    displayUnits.set(item, select.value);
    onChange();
  });
  return select;
}

function fieldInput(value, path, className = "") {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value ?? "";
  input.dataset.path = path;
  input.setAttribute("aria-label", path);
  input.className = className;
  return input;
}

function selectInput(value, choices, path, className = "") {
  const select = document.createElement("select");
  select.dataset.path = path;
  select.setAttribute("aria-label", path);
  select.className = className;
  for (const [choice, label] of choices) {
    const option = document.createElement("option");
    option.value = choice;
    option.textContent = label;
    select.append(option);
  }
  if (![...select.options].some((option) => option.value === value)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `${String(value).replaceAll("_", " ")} (loaded)`;
    select.append(option);
  }
  select.value = value;
  return select;
}

function removeButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "remove-button";
  button.setAttribute("aria-label", label);
  button.title = label;
  button.textContent = "×";
  button.addEventListener("click", handler);
  return button;
}

function invalidateFrozenPlan() {
  latestPreview = null;
  frozenLaunch = null;
  latestRemotePreview = null;
  remoteAuthReady = false;
  byId("freeze-button").disabled = true;
  byId("execution-confirmation").hidden = true;
  byId("remote-preview-controls").hidden = true;
  byId("remote-preview-result").hidden = true;
  byId("remote-preview-button").disabled = true;
  byId("remote-acknowledgement").checked = false;
  byId("remote-auth-button").disabled = false;
  byId("remote-dispatch-button").disabled = true;
  byId("remote-auth-status").textContent = "GitHub access has not been checked.";
  if (trackedJobs.size === 0) byId("launch-result").hidden = true;
  byId("execution-acknowledgement").checked = false;
  byId("execution-acknowledgement").disabled = false;
  byId("start-button").disabled = true;
}

function schedulePreview() {
  if (!recipe) return;
  invalidateFrozenPlan();
  window.clearTimeout(previewTimer);
  const status = byId("preview-status");
  status.className = "status-pill idle preview-pending";
  status.textContent = "Checking";
  previewTimer = window.setTimeout(preview, 350);
}

function setRecipeField(input, object, key, numeric = false) {
  input.addEventListener("input", () => {
    object[key] = numeric ? numericValue(input.value) : input.value;
    schedulePreview();
  });
}

function textCell(value = "—") {
  const cell = document.createElement("td");
  cell.className = "not-applicable";
  cell.textContent = value;
  return cell;
}

function setDistribution(variable, distribution) {
  const previousNominal = Number(variable.nominal);
  const nominal = Number.isFinite(previousNominal) ? previousNominal : 1;
  for (const key of ["minimum", "maximum", "sigma", "values", "weights", "csv_path", "column", "source"]) {
    delete variable[key];
  }
  variable.distribution = distribution;
  if (distribution === "gaussian" || distribution === "uniform") {
    variable.nominal = nominal;
    variable.minimum = nominal * 0.95;
    variable.maximum = nominal * 1.05;
    if (variable.minimum === variable.maximum) {
      variable.minimum = nominal - 1;
      variable.maximum = nominal + 1;
    }
    if (distribution === "gaussian") {
      variable.sigma = Math.abs(variable.maximum - variable.minimum) / 6;
    }
  } else if (distribution === "discrete") {
    const label = String(variable.nominal ?? nominal);
    variable.values = [label];
    variable.weights = [1];
    variable.nominal = label;
  } else {
    delete variable.nominal;
    variable.values = [nominal];
  }
}

function removeCorrelationVariable(name) {
  const groups = recipe.plan.correlations || [];
  for (const group of groups) {
    const index = (group.variables || []).indexOf(name);
    if (index < 0) continue;
    group.variables.splice(index, 1);
    group.matrix.splice(index, 1);
    for (const row of group.matrix) row.splice(index, 1);
  }
  recipe.plan.correlations = groups.filter((group) => group.variables.length >= 2);
}

function schematicContext() {
  if (!recipe.report_context || typeof recipe.report_context !== "object") {
    recipe.report_context = {};
  }
  return recipe.report_context;
}

function renderSchematicErrors(errors = []) {
  const container = byId("schematic-errors");
  if (errors.length === 0) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const list = document.createElement("ul");
  for (const error of errors) {
    const item = document.createElement("li");
    item.textContent = error.message || String(error);
    list.append(item);
  }
  container.replaceChildren(list);
  container.hidden = false;
}

function showSchematicImage(cacheBust = false) {
  if (!recipe) return;
  const context = schematicContext();
  const image = byId("schematic-preview");
  const placeholder = byId("schematic-placeholder");
  const path = String(context.schematic_path || "").trim();
  if (!path) {
    image.hidden = true;
    image.removeAttribute("src");
    placeholder.hidden = false;
    byId("schematic-status").textContent = "Select a PNG/JPEG or capture an LTspice schematic.";
    return;
  }
  image.onload = () => {
    image.hidden = false;
    placeholder.hidden = true;
    byId("schematic-status").textContent = path;
  };
  image.onerror = () => {
    image.hidden = true;
    placeholder.hidden = false;
    byId("schematic-status").textContent = `Image unavailable: ${path}`;
  };
  const version = cacheBust ? `&v=${Date.now()}` : "";
  image.src = `/api/schematic/image?path=${encodeURIComponent(path)}${version}`;
}

function populateSchematicControls() {
  const context = schematicContext();
  byId("schematic-source-path").value = context.schematic_source_path || "";
  byId("schematic-image-path").value = context.schematic_path || "";
  byId("circuit-title").textContent = context.title || recipe.name || "Circuit under study";
  byId("circuit-summary").textContent = context.circuit_summary || recipe.description || "LTspice study schematic";
  renderSchematicErrors();
  showSchematicImage();
}

async function loadSchematicFiles() {
  const response = await fetch("/api/schematic/files");
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || "Schematic files could not be listed");
  const populate = (id, values) => {
    byId(id).replaceChildren(...values.map((value) => {
      const option = document.createElement("option");
      option.value = value;
      return option;
    }));
  };
  populate("schematic-source-files", result.sources || []);
  populate("schematic-image-files", result.images || []);
}

async function loadNetlistFiles() {
  const response = await fetch("/api/recipe/netlists");
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || "Netlist files could not be listed");
  netlistFiles = result.files || [];
  if (recipe) populateExperiments();
}

function netlistSelect(experiment, path) {
  const value = experiment.netlist_path || "";
  const choices = netlistFiles.map((file) => [file, file]);
  if (!value) choices.unshift(["", "— Select a netlist —"]);
  const select = selectInput(value, choices, path);
  select.addEventListener("change", () => {
    experiment.netlist_path = select.value;
    experiment.filename = select.value.split("/").pop() || "";
    populateExperiments();
    schedulePreview();
  });
  return select;
}

async function captureSchematic() {
  if (!recipe) return;
  const sourcePath = byId("schematic-source-path").value.trim();
  const button = byId("capture-schematic");
  renderSchematicErrors();
  if (!sourcePath) {
    renderSchematicErrors([{message: "Select a workspace-relative LTspice .asc file first."}]);
    return;
  }
  button.disabled = true;
  button.textContent = "Capturing…";
  byId("schematic-status").textContent = "Opening LTspice and capturing its schematic window…";
  try {
    const response = await fetch("/api/schematic/capture", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({source_path: sourcePath}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Schematic capture failed");
    const context = schematicContext();
    context.schematic_source_path = result.source_path;
    context.schematic_path = result.schematic_path;
    byId("schematic-source-path").value = result.source_path;
    byId("schematic-image-path").value = result.schematic_path;
    byId("schematic-status").textContent = `${result.capture_method} · ${result.width} × ${result.height}`;
    showSchematicImage(true);
    await loadSchematicFiles();
    schedulePreview();
  } catch (error) {
    renderSchematicErrors([{message: error.message}]);
    byId("schematic-status").textContent = "Capture did not complete.";
  } finally {
    button.disabled = false;
    button.textContent = "Capture from LTspice";
  }
}

function discreteEditor(variable, base) {
  const editor = document.createElement("div");
  editor.className = "distribution-editor";
  const heading = document.createElement("div");
  heading.className = "distribution-editor-heading";
  const title = document.createElement("strong");
  title.textContent = "Discrete choices";
  const note = document.createElement("span");
  note.textContent = "Relative weights are normalized by the plan engine.";
  heading.append(title, note);
  const rows = document.createElement("div");
  rows.className = "choice-rows";
  for (const [index, value] of (variable.values || []).entries()) {
    const row = document.createElement("div");
    row.className = "choice-row";
    const valueInput = fieldInput(value, `${base}.values[${index}]`);
    valueInput.placeholder = "SPICE value or category";
    valueInput.addEventListener("input", () => {
      const previous = variable.values[index];
      variable.values[index] = valueInput.value;
      if (variable.nominal === previous) variable.nominal = valueInput.value;
      schedulePreview();
    });
    const weight = fieldInput(variable.weights?.[index] ?? 1, `${base}.weights[${index}]`);
    weight.placeholder = "Weight";
    setRecipeField(weight, variable.weights, index, true);
    row.append(valueInput, weight, removeButton(`Remove discrete choice ${index + 1}`, () => {
      const removed = variable.values.splice(index, 1)[0];
      variable.weights.splice(index, 1);
      if (variable.nominal === removed) variable.nominal = variable.values[0] ?? "";
      populateVariables();
      schedulePreview();
    }));
    rows.append(row);
  }
  const add = document.createElement("button");
  add.type = "button";
  add.className = "compact-button";
  add.textContent = "+ Choice";
  add.addEventListener("click", () => {
    let suffix = variable.values.length + 1;
    while (variable.values.includes(`value_${suffix}`)) suffix += 1;
    variable.values.push(`value_${suffix}`);
    variable.weights.push(1);
    populateVariables();
    schedulePreview();
  });
  rows.append(add);
  editor.append(heading, rows);
  return editor;
}

function empiricalEditor(variable, base) {
  const editor = document.createElement("div");
  editor.className = "distribution-editor";
  const heading = document.createElement("div");
  heading.className = "distribution-editor-heading";
  const title = document.createElement("strong");
  title.textContent = "Measured population";
  const mode = selectInput(
    variable.csv_path || variable.source?.kind === "csv" ? "csv" : "inline",
    [["inline", "Inline observations"], ["csv", "Workspace CSV"]],
    `${base}.empirical_mode`,
  );
  mode.addEventListener("change", () => {
    delete variable.source;
    if (mode.value === "csv") {
      delete variable.values;
      variable.csv_path = "examples/measurements.csv";
      variable.column = "value";
    } else {
      delete variable.csv_path;
      delete variable.column;
      variable.values = [1];
    }
    populateVariables();
    schedulePreview();
  });
  heading.append(title, mode);
  editor.append(heading);
  if (mode.value === "csv") {
    const fields = document.createElement("div");
    fields.className = "compact-fields";
    for (const [key, label] of [["csv_path", "Workspace-relative CSV"], ["column", "Column"]]) {
      const wrapper = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = label;
      const input = fieldInput(variable[key] ?? variable.source?.[key] ?? "", `${base}.${key}`);
      setRecipeField(input, variable, key);
      wrapper.append(caption, input);
      fields.append(wrapper);
    }
    editor.append(fields);
  } else {
    if (variable.source) delete variable.source;
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = "Observations (comma or line separated)";
    const values = document.createElement("textarea");
    values.dataset.path = `${base}.values`;
    values.setAttribute("aria-label", `${base}.values`);
    values.value = (variable.values || []).join("\n");
    values.addEventListener("input", () => {
      variable.values = values.value
        .split(/[\n,]/)
        .map((value) => value.trim())
        .filter(Boolean)
        .map(numericValue);
      schedulePreview();
    });
    label.append(caption, values);
    editor.append(label);
  }
  return editor;
}

function populateVariables() {
  const variables = recipe.plan.variables || [];
  byId("variable-count").textContent = `${variables.length} variables`;
  const rows = variables.map((variable, index) => {
    const base = `plan.variables[${index}]`;
    const row = document.createElement("tr");
    row.dataset.path = base;
    const name = fieldInput(variable.name, `${base}.name`, "variable-name");
    name.addEventListener("input", () => {
      const previous = variable.name;
      variable.name = name.value;
      for (const group of recipe.plan.correlations || []) {
        group.variables = group.variables.map((entry) => entry === previous ? name.value : entry);
      }
      populateCorrelations();
      schedulePreview();
    });
    const distribution = selectInput(variable.distribution, [
      ["gaussian", "Gaussian"],
      ["uniform", "Uniform"],
      ["discrete", "Discrete"],
      ["empirical", "Empirical"],
    ], `${base}.distribution`, "distribution");
    distribution.addEventListener("change", () => {
      if (distribution.value !== "gaussian") {
        removeCorrelationVariable(variable.name);
      }
      setDistribution(variable, distribution.value);
      populateVariables();
      populateCorrelations();
      schedulePreview();
    });
    const continuous = ["gaussian", "uniform"].includes(variable.distribution);
    let nominal;
    let tolerance;
    let minimum;
    let maximum;
    let unit;
    if (continuous) {
      const selectedUnit = variableDisplayUnits.get(variable) || defaultDisplayUnit(variable);
      const factor = studyUnitFactor(variable.unit, selectedUnit);
      nominal = scaledFieldInput(variable.nominal, factor, `${base}.nominal`);
      setScaledRecipeField(nominal, variable, "nominal", factor);
      tolerance = scaledFieldInput(variable.sigma, factor, `${base}.sigma`);
      tolerance.placeholder = distribution.value === "gaussian" ? "σ" : "n/a";
      tolerance.disabled = distribution.value !== "gaussian";
      if (!tolerance.disabled) setScaledRecipeField(tolerance, variable, "sigma", factor);
      minimum = scaledFieldInput(variable.minimum, factor, `${base}.minimum`);
      setScaledRecipeField(minimum, variable, "minimum", factor);
      maximum = scaledFieldInput(variable.maximum, factor, `${base}.maximum`);
      setScaledRecipeField(maximum, variable, "maximum", factor);
      unit = unitSelect(variable, variableDisplayUnits, `${base}.display_unit`, populateVariables);
    } else if (variable.distribution === "discrete") {
      nominal = selectInput(
        variable.nominal,
        (variable.values || []).map((value) => [value, value]),
        `${base}.nominal`,
      );
      nominal.addEventListener("change", () => {
        variable.nominal = nominal.value;
        schedulePreview();
      });
    }
    if (!unit) {
      unit = fieldInput(variable.unit, `${base}.unit`, "unit");
      setRecipeField(unit, variable, "unit");
    }
    for (const control of [name, distribution]) {
      const cell = document.createElement("td");
      cell.append(control);
      row.append(cell);
    }
    for (const control of [nominal, tolerance, minimum, maximum]) {
      if (control) {
        const cell = document.createElement("td");
        cell.append(control);
        row.append(cell);
      } else {
        row.append(textCell());
      }
    }
    const unitCell = document.createElement("td");
    unitCell.append(unit);
    row.append(unitCell);
    const remove = document.createElement("td");
    remove.className = "remove-cell";
    remove.append(removeButton(`Remove variable ${variable.name || index + 1}`, () => {
      removeCorrelationVariable(variable.name);
      variables.splice(index, 1);
      populateVariables();
      populateCorrelations();
      schedulePreview();
    }));
    row.append(remove);
    if (variable.distribution === "discrete" || variable.distribution === "empirical") {
      const details = document.createElement("tr");
      details.className = "distribution-detail-row";
      details.dataset.path = base;
      const cell = document.createElement("td");
      cell.colSpan = 8;
      cell.append(variable.distribution === "discrete"
        ? discreteEditor(variable, base)
        : empiricalEditor(variable, base));
      details.append(cell);
      return [row, details];
    }
    return [row];
  });
  byId("variables").replaceChildren(...rows.flat());
}

function populateCorrelations() {
  const groups = recipe.plan.correlations || (recipe.plan.correlations = []);
  const variables = recipe.plan.variables || [];
  const gaussian = variables.filter((variable) => variable.distribution === "gaussian");
  const cards = groups.map((group, groupIndex) => {
    const base = `plan.correlations[${groupIndex}]`;
    const card = document.createElement("section");
    card.className = "editor-card correlation-card";
    card.dataset.path = base;
    const heading = document.createElement("div");
    heading.className = "editor-card-heading";
    const title = document.createElement("strong");
    title.textContent = `Correlation group ${groupIndex + 1}`;
    heading.append(title, removeButton(`Remove correlation group ${groupIndex + 1}`, () => {
      groups.splice(groupIndex, 1);
      populateCorrelations();
      schedulePreview();
    }));
    const choices = document.createElement("div");
    choices.className = "correlation-variables";
    for (const variable of variables) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = (group.variables || []).includes(variable.name);
      checkbox.disabled = variable.distribution !== "gaussian" && !checkbox.checked;
      checkbox.addEventListener("change", () => {
        const oldNames = [...group.variables];
        const oldMatrix = group.matrix.map((row) => [...row]);
        if (checkbox.checked) group.variables.push(variable.name);
        else group.variables = group.variables.filter((name) => name !== variable.name);
        group.matrix = group.variables.map((rowName, rowIndex) =>
          group.variables.map((columnName, columnIndex) => {
            if (rowName === columnName) return 1;
            const oldRow = oldNames.indexOf(rowName);
            const oldColumn = oldNames.indexOf(columnName);
            return oldRow >= 0 && oldColumn >= 0 ? oldMatrix[oldRow][oldColumn] : 0;
          }));
        populateCorrelations();
        schedulePreview();
      });
      label.append(checkbox, document.createTextNode(variable.name));
      choices.append(label);
    }
    const matrix = document.createElement("div");
    matrix.className = "correlation-matrix";
    matrix.style.setProperty("--matrix-size", String(Math.max(1, group.variables.length + 1)));
    matrix.append(document.createElement("span"));
    for (const name of group.variables) {
      const label = document.createElement("strong");
      label.textContent = name;
      matrix.append(label);
    }
    for (const [rowIndex, rowName] of group.variables.entries()) {
      const label = document.createElement("strong");
      label.textContent = rowName;
      matrix.append(label);
      for (const [columnIndex] of group.variables.entries()) {
        const input = fieldInput(group.matrix?.[rowIndex]?.[columnIndex] ?? (rowIndex === columnIndex ? 1 : 0), `${base}.matrix[${rowIndex}][${columnIndex}]`);
        input.disabled = columnIndex >= rowIndex;
        if (columnIndex < rowIndex) {
          input.addEventListener("input", () => {
            const value = numericValue(input.value);
            group.matrix[rowIndex][columnIndex] = value;
            group.matrix[columnIndex][rowIndex] = value;
            const mirrorPath = `${base}.matrix[${columnIndex}][${rowIndex}]`;
            const mirror = [...matrix.querySelectorAll("input")]
              .find((element) => element.dataset.path === mirrorPath);
            if (mirror) mirror.value = input.value;
            schedulePreview();
          });
        }
        matrix.append(input);
      }
    }
    card.append(heading, choices, matrix);
    return card;
  });
  byId("correlations").replaceChildren(...(cards.length ? cards : [emptyEditor("No matched-variable correlation groups defined.")]));
  const used = new Set(groups.flatMap((group) => group.variables || []));
  byId("add-correlation").disabled = gaussian.filter((variable) => !used.has(variable.name)).length < 2;
}

function populateCorners() {
  const axes = recipe.plan.corner_axes || (recipe.plan.corner_axes = []);
  const cards = axes.map((axis, axisIndex) => {
    const base = `plan.corner_axes[${axisIndex}]`;
    const card = document.createElement("section");
    card.className = "editor-card";
    card.dataset.path = base;
    const heading = document.createElement("div");
    heading.className = "editor-card-heading";
    const title = document.createElement("strong");
    title.textContent = axis.name || `Corner axis ${axisIndex + 1}`;
    heading.append(title, removeButton(`Remove corner axis ${axis.name || axisIndex + 1}`, () => {
      axes.splice(axisIndex, 1);
      populateCorners();
      schedulePreview();
    }));
    const fields = document.createElement("div");
    fields.className = "compact-fields";
    for (const [key, label] of [["name", "Axis name"], ["parameter", "Netlist parameter"]]) {
      const wrapper = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = label;
      const input = fieldInput(axis[key], `${base}.${key}`);
      setRecipeField(input, axis, key);
      wrapper.append(caption, input);
      fields.append(wrapper);
    }
    const unitWrapper = document.createElement("label");
    const unitCaption = document.createElement("span");
    unitCaption.textContent = "Display unit";
    let unit = unitSelect(axis, cornerDisplayUnits, `${base}.display_unit`, populateCorners);
    if (!unit) {
      unit = fieldInput(axis.unit, `${base}.unit`);
      setRecipeField(unit, axis, "unit");
    }
    unitWrapper.append(unitCaption, unit);
    fields.append(unitWrapper);
    const selectedUnit = cornerDisplayUnits.get(axis) || defaultDisplayUnit(axis);
    const factor = studyUnitFactor(axis.unit, selectedUnit);
    const values = document.createElement("div");
    values.className = "corner-values";
    for (const [valueIndex, entry] of (axis.values || []).entries()) {
      const valueBase = `${base}.values[${valueIndex}]`;
      const row = document.createElement("div");
      row.className = "corner-value-row";
      row.dataset.path = valueBase;
      const label = fieldInput(entry.name, `${valueBase}.name`);
      label.placeholder = "Corner label";
      setRecipeField(label, entry, "name");
      const value = scaledFieldInput(entry.value, factor, `${valueBase}.value`);
      value.placeholder = "Value";
      setScaledRecipeField(value, entry, "value", factor);
      row.append(label, value, removeButton(`Remove ${entry.name || "corner value"}`, () => {
        axis.values.splice(valueIndex, 1);
        populateCorners();
        schedulePreview();
      }));
      values.append(row);
    }
    const add = document.createElement("button");
    add.type = "button";
    add.className = "compact-button";
    add.textContent = "+ Value";
    add.addEventListener("click", () => {
      axis.values.push({name: `value_${axis.values.length + 1}`, value: 0});
      populateCorners();
      schedulePreview();
    });
    values.append(add);
    card.append(heading, fields, values);
    return card;
  });
  byId("corners").replaceChildren(...(cards.length ? cards : [emptyEditor("No operating-corner axes defined.")]));
}

function populateExperiments() {
  const experiments = recipe.experiments || (recipe.experiments = []);
  let requirementCount = 0;

  const groups = experiments.map((experiment, experimentIndex) => {
    const base = `experiments[${experimentIndex}]`;
    const group = document.createElement("section");
    group.className = "editor-card experiment-group";
    group.dataset.path = base;

    const heading = document.createElement("div");
    heading.className = "editor-card-heading";
    const title = document.createElement("strong");
    title.textContent = experiment.name || `Experiment ${experimentIndex + 1}`;
    heading.append(title, removeButton(`Remove experiment ${experiment.name || experimentIndex + 1}`, () => {
      experiments.splice(experimentIndex, 1);
      populateExperiments();
      schedulePreview();
    }));

    const fields = document.createElement("div");
    fields.className = "compact-fields";
    const nameWrapper = document.createElement("label");
    const nameCaption = document.createElement("span");
    nameCaption.textContent = "Experiment name";
    const nameField = fieldInput(experiment.name, `${base}.name`);
    nameField.addEventListener("input", () => {
      experiment.name = nameField.value;
      title.textContent = experiment.name || `Experiment ${experimentIndex + 1}`;
      schedulePreview();
    });
    nameWrapper.append(nameCaption, nameField);
    fields.append(nameWrapper);
    if (experiments.length > 1) {
      const netlistWrapper = document.createElement("label");
      const netlistCaption = document.createElement("span");
      netlistCaption.textContent = "Netlist (.cir/.net)";
      netlistWrapper.append(netlistCaption, netlistSelect(experiment, `${base}.netlist_path`));
      fields.append(netlistWrapper);
    }

    const analyses = experiment.waveform_analyses || (experiment.waveform_analyses = []);
    const analysisStack = document.createElement("div");
    analysisStack.className = "editor-stack analysis-stack";
    for (const [analysisIndex, analysis] of analyses.entries()) {
      const analysisBase = `${base}.waveform_analyses[${analysisIndex}]`;
      const card = document.createElement("section");
      card.className = "editor-card analysis-card";
      card.dataset.path = analysisBase;

      const analysisHeading = document.createElement("div");
      analysisHeading.className = "editor-card-heading";
      const analysisTitle = document.createElement("strong");
      analysisTitle.textContent = analysis.name || `Analysis ${analysisIndex + 1}`;
      analysisHeading.append(analysisTitle, removeButton(`Remove ${analysis.name || "analysis"}`, () => {
        analyses.splice(analysisIndex, 1);
        populateExperiments();
        schedulePreview();
      }));

      const analysisFields = document.createElement("div");
      analysisFields.className = "compact-fields";
      for (const [key, label, placeholder] of [
        ["name", "Analysis name", "response"],
        ["variable", "Signal, e.g. V(out)", "V(out)"],
        ["secondary_variable", "Reference signal (optional)", "V(in)"],
      ]) {
        const wrapper = document.createElement("label");
        const caption = document.createElement("span");
        caption.textContent = label;
        const input = fieldInput(analysis[key], `${analysisBase}.${key}`);
        input.placeholder = placeholder;
        input.addEventListener("input", () => {
          if (key === "name") {
            analysis.name = input.value;
            analysisTitle.textContent = analysis.name || `Analysis ${analysisIndex + 1}`;
          } else {
            const value = input.value.trim();
            if (value) analysis[key] = value;
            else delete analysis[key];
          }
          schedulePreview();
        });
        wrapper.append(caption, input);
        analysisFields.append(wrapper);
      }

      const rows = document.createElement("div");
      rows.className = "requirement-rows";
      for (const [requirementIndex, requirement] of (analysis.requirements || []).entries()) {
        requirementCount += 1;
        const requirementBase = `${analysisBase}.requirements[${requirementIndex}]`;
        const row = document.createElement("div");
        row.className = "requirement-row";
        row.dataset.path = requirementBase;
        const metric = fieldInput(requirement.metric, `${requirementBase}.metric`);
        metric.placeholder = "Metric";
        setRecipeField(metric, requirement, "metric");
        const operator = selectInput(requirement.operator, [["<", "<"], ["<=", "≤"], [">", ">"], [">=", "≥"]], `${requirementBase}.operator`);
        setRecipeField(operator, requirement, "operator");
        operator.addEventListener("change", schedulePreview);
        const target = fieldInput(requirement.target, `${requirementBase}.target`);
        target.placeholder = "Target";
        setRecipeField(target, requirement, "target", true);
        row.append(metric, operator, target, removeButton(`Remove ${requirement.metric} requirement`, () => {
          analysis.requirements.splice(requirementIndex, 1);
          populateExperiments();
          schedulePreview();
        }));
        rows.append(row);
      }
      const addRequirement = document.createElement("button");
      addRequirement.type = "button";
      addRequirement.className = "compact-button";
      addRequirement.textContent = "+ Requirement";
      addRequirement.addEventListener("click", () => {
        (analysis.requirements || (analysis.requirements = [])).push({metric: "maximum", operator: "<=", target: 0});
        populateExperiments();
        schedulePreview();
      });
      rows.append(addRequirement);
      card.append(analysisHeading, analysisFields, rows);
      analysisStack.append(card);
    }

    const addAnalysis = document.createElement("button");
    addAnalysis.type = "button";
    addAnalysis.className = "compact-button";
    addAnalysis.textContent = "+ Analysis";
    addAnalysis.addEventListener("click", () => {
      const names = new Set(analyses.map((analysis) => analysis.name));
      let suffix = analyses.length + 1;
      while (names.has(`analysis_${suffix}`)) suffix += 1;
      analyses.push({
        name: `analysis_${suffix}`,
        variable: "V(out)",
        requirements: [{metric: "maximum", operator: "<=", target: 0}],
      });
      populateExperiments();
      schedulePreview();
    });

    group.append(heading, fields, analysisStack, addAnalysis);
    return group;
  });

  byId("requirement-count").textContent = `${requirementCount} requirements`;
  byId("requirements").replaceChildren(...(groups.length ? groups : [emptyEditor("No experiments defined.")]));
  populatePrimaryNetlist(experiments);
}

function populatePrimaryNetlist(experiments) {
  const row = byId("primary-netlist-row");
  const note = byId("primary-netlist-note");
  if (experiments.length === 1) {
    row.hidden = false;
    note.hidden = true;
    byId("primary-netlist-field").replaceChildren(
      netlistSelect(experiments[0], "experiments[0].netlist_path")
    );
  } else {
    row.hidden = true;
    note.hidden = experiments.length === 0;
  }
}

function emptyEditor(message) {
  const empty = document.createElement("p");
  empty.className = "editor-empty";
  empty.textContent = message;
  return empty;
}

function populateRecipeControls() {
  byId("study-name").textContent = recipe.name || "Untitled study";
  byId("study-description").textContent = recipe.description || "Portable LTspice study recipe";
  byId("sample-count").value = recipe.plan.sample_count;
  byId("seed").value = recipe.plan.seed;
  byId("sampling-method").value = recipe.plan.sampling_method || "independent";
  populateVariables();
  populateCorrelations();
  populateCorners();
  populateExperiments();
  populateSchematicControls();
}

function renderErrors(errors) {
  const container = byId("errors");
  if (!errors || errors.length === 0) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const title = document.createElement("strong");
  title.textContent = "Resolve these definition errors";
  const list = document.createElement("ul");
  for (const error of errors) {
    const item = document.createElement("li");
    item.textContent = `${error.path}: ${error.message}`;
    list.append(item);
  }
  container.replaceChildren(title, list);
  container.hidden = false;
}

function renderScopedErrors(errors) {
  const scopes = [
    ["variable-errors", ["plan.variables"]],
    ["correlation-errors", ["plan.correlations"]],
    ["corner-errors", ["plan.corner_axes"]],
    ["requirement-errors", ["experiments"]],
    ["schematic-errors", ["report_context.schematic_path", "report_context.schematic_source_path"]],
  ];
  document.querySelectorAll("[data-path]").forEach((element) => {
    element.removeAttribute("aria-invalid");
  });
  for (const [id, prefixes] of scopes) {
    const matched = (errors || []).filter((error) => prefixes.some((prefix) => error.path.startsWith(prefix)));
    const container = byId(id);
    if (matched.length === 0) {
      container.hidden = true;
      container.replaceChildren();
      continue;
    }
    const list = document.createElement("ul");
    for (const error of matched) {
      const item = document.createElement("li");
      item.textContent = `${error.path}: ${error.message}`;
      list.append(item);
      document.querySelectorAll("[data-path]").forEach((element) => {
        const path = element.dataset.path;
        if (error.path.startsWith(path) || path.startsWith(error.path)) {
          element.setAttribute("aria-invalid", "true");
        }
      });
    }
    container.replaceChildren(list);
    container.hidden = false;
  }
}

function clearPreviewMetrics() {
  for (const id of ["metric-samples", "metric-corners", "metric-points", "metric-runs"]) {
    byId(id).textContent = "—";
  }
  byId("plan-id").textContent = "Not generated";
  byId("experiments").replaceChildren();
}

function renderPreview(result) {
  const status = byId("preview-status");
  if (!result.valid) {
    latestPreview = null;
    byId("freeze-button").disabled = true;
    status.className = "status-pill invalid";
    status.textContent = "Needs attention";
    byId("preview-title").textContent = "Definition is not valid";
    renderErrors(result.errors);
    renderScopedErrors(result.errors);
    clearPreviewMetrics();
    return;
  }
  status.className = "status-pill valid";
  status.textContent = "Valid";
  byId("preview-title").textContent = "Ready to become immutable";
  byId("metric-samples").textContent = result.plan.sample_count.toLocaleString();
  byId("metric-corners").textContent = result.plan.corner_combination_count.toLocaleString();
  byId("metric-points").textContent = result.plan.point_count.toLocaleString();
  byId("metric-runs").textContent = result.execution.total_run_count.toLocaleString();
  byId("plan-id").textContent = result.plan.plan_id;
  latestPreview = result;
  byId("freeze-button").disabled = false;
  renderErrors([]);
  renderScopedErrors([]);
  byId("experiments").replaceChildren(...result.experiments.map((experiment) => {
    const card = document.createElement("div");
    card.className = "experiment";
    const icon = document.createElement("span");
    icon.className = "experiment-icon";
    icon.textContent = experiment.name.slice(0, 2).toUpperCase();
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    const detail = document.createElement("small");
    title.textContent = experiment.name;
    detail.textContent = `${experiment.analysis_count} analyses · ${experiment.requirement_count} requirements`;
    copy.append(title, detail);
    const runs = document.createElement("span");
    runs.className = "run-count";
    runs.textContent = `${result.plan.point_count} runs`;
    card.append(icon, copy, runs);
    return card;
  }));
}

function relativeTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown time";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absolute = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat(undefined, {numeric: "auto"});
  if (absolute < 60) return formatter.format(seconds, "second");
  if (absolute < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (absolute < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

function statusClass(status) {
  if (status === "completed") return "completed";
  if (["running", "queued", "cancelling"].includes(status)) return "active";
  if (status === "defined") return "defined";
  return "failed";
}

function emptyHistory(message) {
  const element = document.createElement("p");
  element.className = "empty-history";
  element.textContent = message;
  return element;
}

function reportLink(url, label = "Open report ↗") {
  const link = document.createElement("a");
  link.className = "report-link";
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = label;
  return link;
}

function jobActionButton(label, action, className = "compact-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await action();
    } catch (error) {
      renderHistoryErrors([{path: "job action", message: error.message}]);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

async function mutateJob(experimentId, action) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(experimentId)}/${action}`, {
    method: "POST",
    headers: {"X-LTspice-System-Builder": "1"},
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || `${action} failed`);
  return result;
}

function renderTrackedJobs() {
  const container = byId("launch-result");
  if (trackedJobs.size === 0) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const title = document.createElement("h3");
  title.textContent = "Durable local execution";
  const cards = [...trackedJobs.values()].map((job) => {
    const card = document.createElement("div");
    card.className = "tracked-job";
    const heading = document.createElement("div");
    const identity = document.createElement("strong");
    identity.textContent = String(job.name || "experiment").toUpperCase();
    const status = document.createElement("span");
    status.className = `job-status ${statusClass(job.status)}`;
    status.textContent = job.finalizing ? "building report" : job.status;
    heading.append(identity, status);
    const progress = document.createElement("div");
    progress.className = "progress-track";
    const bar = document.createElement("span");
    const finished = Number(job.finished_points || 0);
    const total = Number(job.point_count || 0);
    bar.style.width = `${total ? Math.min(100, finished / total * 100) : 0}%`;
    progress.append(bar);
    const detail = document.createElement("small");
    detail.textContent = `${finished}/${total} points · ${job.passed_points || 0} pass · ${job.failed_points || 0} fail`;
    const id = document.createElement("code");
    id.textContent = job.experiment_id;
    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (["queued", "running", "cancelling"].includes(job.status)) {
      actions.append(jobActionButton("Cancel", async () => {
        trackedJobs.set(job.experiment_id, {...job, ...await mutateJob(job.experiment_id, "cancel")});
        renderTrackedJobs();
        scheduleJobPoll(250);
      }));
    }
    if (job.status === "cancelled") {
      actions.append(jobActionButton("Resume unfinished", async () => {
        trackedJobs.set(job.experiment_id, {...job, ...await mutateJob(job.experiment_id, "resume")});
        renderTrackedJobs();
        scheduleJobPoll(250);
      }));
    }
    if (job.report_url) actions.append(reportLink(job.report_url));
    if (job.postprocess_error) {
      const error = document.createElement("span");
      error.className = "job-error";
      error.textContent = job.postprocess_error;
      actions.append(error);
    }
    card.append(heading, progress, detail, id, actions);
    return card;
  });
  container.replaceChildren(title, ...cards);
  container.hidden = false;
}

async function refreshTrackedJob(job) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(job.experiment_id)}`);
  const current = await response.json();
  if (!response.ok) throw new Error(current.error?.message || "Job status could not be read");
  const updated = {
    ...job,
    ...current,
    finalizing: current.status === "completed"
      && !current.report_available
      && current.postprocess?.state !== "failed",
    postprocess_error: current.postprocess?.error || null,
  };
  trackedJobs.set(job.experiment_id, updated);
}

async function pollTrackedJobs() {
  jobPollTimer = null;
  try {
    await Promise.all([...trackedJobs.values()].map(refreshTrackedJob));
    renderTrackedJobs();
    await loadHistory(false);
  } catch (error) {
    renderHistoryErrors([{path: "durable execution", message: error.message}]);
  }
  if ([...trackedJobs.values()].some((job) => ["defined", "queued", "running", "cancelling"].includes(job.status) || job.finalizing)) {
    scheduleJobPoll(1000);
  }
}

function scheduleJobPoll(delay = 1000) {
  window.clearTimeout(jobPollTimer);
  jobPollTimer = window.setTimeout(pollTrackedJobs, delay);
}

function renderHistory(result) {
  byId("history-total").textContent = result.summary.total_jobs.toLocaleString();
  byId("history-active").textContent = result.summary.active_jobs.toLocaleString();
  byId("history-reports").textContent = result.summary.reports.toLocaleString();
  const index = byId("history-index");
  index.textContent = result.index.current ? "Current" : (result.index.available ? "Stale" : "Unavailable");
  index.className = result.index.current ? "index-ready" : "index-missing";
  index.title = result.index.message;

  const jobs = result.jobs.map((job) => {
    const row = document.createElement("div");
    row.className = "history-item";
    const top = document.createElement("div");
    top.className = "history-item-top";
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    const meta = document.createElement("small");
    title.textContent = job.statistical ? "Statistical experiment" : "Experiment";
    meta.textContent = `${relativeTime(job.recorded_at)} · ${job.execution_mode} execution`;
    identity.append(title, meta);
    const status = document.createElement("span");
    status.className = `job-status ${statusClass(job.status)}`;
    status.textContent = job.status;
    top.append(identity, status);

    const progress = document.createElement("div");
    progress.className = "progress-track";
    const progressValue = document.createElement("span");
    const percentage = job.point_count > 0 ? (job.finished_points / job.point_count) * 100 : 0;
    progressValue.style.width = `${Math.min(100, percentage)}%`;
    progress.append(progressValue);

    const bottom = document.createElement("div");
    bottom.className = "history-item-bottom";
    const details = document.createElement("span");
    details.textContent = `${job.finished_points}/${job.point_count} points · ${job.passed_points} pass · ${job.failed_points} fail`;
    bottom.append(details);
    if (job.report_url) bottom.append(reportLink(job.report_url));
    if (["queued", "running", "cancelling"].includes(job.status)) {
      bottom.append(jobActionButton("Cancel", async () => {
        trackedJobs.set(job.experiment_id, {name: "recovered", ...job, ...await mutateJob(job.experiment_id, "cancel")});
        renderTrackedJobs();
        scheduleJobPoll(250);
      }));
    } else if (job.status === "cancelled") {
      bottom.append(jobActionButton("Resume unfinished", async () => {
        trackedJobs.set(job.experiment_id, {name: "resumed", ...job, ...await mutateJob(job.experiment_id, "resume")});
        renderTrackedJobs();
        scheduleJobPoll(250);
      }));
    } else if (job.status === "completed" && !job.report_url) {
      bottom.append(jobActionButton("Build report", async () => {
        await mutateJob(job.experiment_id, "finalize");
        await loadHistory();
      }));
    }

    const id = document.createElement("code");
    id.textContent = job.experiment_id;
    row.append(top, progress, bottom, id);
    return row;
  });
  byId("job-history").replaceChildren(...(jobs.length ? jobs : [emptyHistory("No durable experiments found.")]));
  for (const job of result.jobs.filter((item) => ["queued", "running", "cancelling"].includes(item.status))) {
    if (!trackedJobs.has(job.experiment_id)) trackedJobs.set(job.experiment_id, {name: "recovered", ...job});
  }
  if ([...trackedJobs.values()].some((job) => ["queued", "running", "cancelling"].includes(job.status))) {
    renderTrackedJobs();
    scheduleJobPoll();
  }

  const studies = result.studies.map((study) => {
    const row = document.createElement("div");
    row.className = "history-item study-item";
    const top = document.createElement("div");
    top.className = "history-item-top";
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    const meta = document.createElement("small");
    title.textContent = study.kind === "robust_selection" ? "Robust selection" : "Optimization study";
    meta.textContent = `${study.candidate_count} candidates · ${study.feasible_count ?? "—"} feasible`;
    identity.append(title, meta);
    top.append(identity, reportLink(study.report_url));
    const id = document.createElement("code");
    id.textContent = study.study_id;
    row.append(top, id);
    return row;
  });
  byId("study-history").replaceChildren(...(studies.length ? studies : [emptyHistory("No indexed decision reports found.")]));

  const messages = result.issues.map((issue) => ({path: issue.artifact, message: issue.message}));
  renderHistoryErrors(messages);
}

function renderHistoryErrors(errors) {
  const container = byId("history-errors");
  if (!errors || errors.length === 0) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const title = document.createElement("strong");
  title.textContent = "Some workspace artifacts could not be read";
  const list = document.createElement("ul");
  for (const error of errors) {
    const item = document.createElement("li");
    item.textContent = `${error.path}: ${error.message}`;
    list.append(item);
  }
  container.replaceChildren(title, list);
  container.hidden = false;
}

async function loadHistory(showBusy = true) {
  const button = byId("refresh-history");
  if (showBusy) {
    button.disabled = true;
    button.textContent = "Refreshing…";
  }
  try {
    const response = await fetch("/api/history?limit=12");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "History could not be read");
    renderHistory(result);
  } catch (error) {
    renderHistoryErrors([{path: "workspace", message: error.message}]);
  } finally {
    if (showBusy) {
      button.disabled = false;
      button.textContent = "Refresh status";
    }
  }
}

async function preview() {
  if (!recipe) return;
  const sequence = ++previewSequence;
  updateRecipeFromControls();
  const button = byId("preview-button");
  button.disabled = true;
  button.textContent = "Resolving…";
  try {
    const response = await fetch("/api/preview", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify(recipe),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Preview failed");
    if (sequence !== previewSequence) return;
    renderPreview(result);
  } catch (error) {
    if (sequence !== previewSequence) return;
    renderPreview({valid: false, errors: [{path: "$", message: error.message}]});
  } finally {
    if (sequence === previewSequence) {
      button.disabled = false;
      button.textContent = "Preview resolved plan";
    }
  }
}

async function freezePlan() {
  if (!recipe || !latestPreview) return;
  const button = byId("freeze-button");
  button.disabled = true;
  button.textContent = "Freezing…";
  try {
    const response = await fetch("/api/freeze", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        recipe,
        expected_recipe_sha256: latestPreview.recipe.sha256,
        expected_plan_id: latestPreview.plan.plan_id,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      if (result.valid === false) {
        renderPreview(result);
        return;
      }
      throw new Error(result.error?.message || "Plan could not be frozen");
    }
    frozenLaunch = result;
    byId("frozen-plan-id").textContent = result.plan.plan_id;
    byId("confirm-points").textContent = result.plan.point_count.toLocaleString();
    byId("confirm-experiments").textContent = result.execution.experiment_count.toLocaleString();
    byId("confirm-runs").textContent = result.execution.total_run_count.toLocaleString();
    byId("confirm-concurrency").textContent = result.execution.max_concurrency.toLocaleString();
    byId("frozen-artifact").textContent = result.plan.artifact;
    byId("execution-acknowledgement").checked = false;
    byId("start-button").disabled = true;
    byId("execution-confirmation").hidden = false;
    byId("remote-preview-controls").hidden = false;
    byId("remote-preview-result").hidden = true;
    byId("remote-preview-button").disabled = false;
    byId("launch-result").hidden = true;
    renderErrors([]);
  } catch (error) {
    renderErrors([{path: "freeze", message: error.message}]);
    button.disabled = latestPreview === null;
  } finally {
    button.textContent = "Create immutable plan";
  }
}

async function previewRemoteExecution() {
  if (!frozenLaunch) return;
  const button = byId("remote-preview-button");
  button.disabled = true;
  button.textContent = "Resolving…";
  try {
    const response = await fetch("/api/remote/preview", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        launch_token: frozenLaunch.launch_token,
        confirmed_plan_id: frozenLaunch.plan.plan_id,
        confirmed_run_count: frozenLaunch.execution.total_run_count,
        repository: byId("remote-repository").value.trim(),
        ref: byId("remote-ref").value.trim(),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Remote preview failed");
    latestRemotePreview = result;
    remoteAuthReady = false;
    byId("remote-target-repository").textContent = result.target.repository;
    byId("remote-target-ref").textContent = result.target.ref;
    byId("remote-target-runner").textContent = result.target.runner;
    byId("remote-plan-id").textContent = result.plan.plan_id;
    byId("remote-run-count").textContent = result.workload.total_run_count.toLocaleString();
    byId("remote-retention").textContent = `${result.evidence.retention_days} days`;
    byId("remote-preview-id").textContent = `${result.preview_id} · ${result.preview_sha256}`;
    byId("remote-evidence-formats").textContent = `Expected evidence: ${result.evidence.formats.join(", ")}. Nothing is sent until the separate acknowledgement and dispatch action.`;
    byId("remote-acknowledgement").checked = false;
    byId("remote-dispatch-button").disabled = true;
    byId("remote-auth-status").textContent = "GitHub access has not been checked.";
    byId("remote-mode-status").className = "status-pill idle";
    byId("remote-mode-status").textContent = "Awaiting authorization";
    byId("remote-preview-result").hidden = false;
    renderErrors([]);
  } catch (error) {
    latestRemotePreview = null;
    remoteAuthReady = false;
    renderErrors([{path: "remote_preview", message: error.message}]);
    byId("remote-preview-result").hidden = true;
  } finally {
    button.disabled = frozenLaunch === null;
    button.textContent = "Preview GitHub workload";
  }
}

function updateRemoteDispatchGate() {
  byId("remote-dispatch-button").disabled = !(
    latestRemotePreview
    && remoteAuthReady
    && byId("remote-acknowledgement").checked
  );
}

async function checkRemoteAuth() {
  const button = byId("remote-auth-button");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const response = await fetch("/api/remote/auth", {
      method: "POST",
      headers: {"X-LTspice-System-Builder": "1"},
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "GitHub access check failed");
    remoteAuthReady = result.available === true;
    if (remoteAuthReady) {
      byId("remote-auth-status").textContent = "GitHub CLI is authenticated. Its credential remains outside System Builder.";
      byId("remote-mode-status").className = "status-pill valid";
      byId("remote-mode-status").textContent = "Access verified";
    } else {
      // The server currently only ever answers 200 with available: true, or
      // a non-200 error caught below — but the status text should reflect
      // the flag it just checked rather than assume it, in case that ever
      // changes.
      byId("remote-auth-status").textContent = "GitHub CLI reported no access. Run `gh auth login` and try again.";
      byId("remote-mode-status").className = "status-pill invalid";
      byId("remote-mode-status").textContent = "Access unavailable";
    }
    renderErrors([]);
  } catch (error) {
    remoteAuthReady = false;
    byId("remote-auth-status").textContent = error.message;
    byId("remote-mode-status").className = "status-pill invalid";
    byId("remote-mode-status").textContent = "Access unavailable";
  } finally {
    button.disabled = false;
    button.textContent = "Check GitHub access";
    updateRemoteDispatchGate();
  }
}

function remoteJobStatusClass(job) {
  if (job.state === "evidence_verified" || job.conclusion === "success") return "completed";
  if (job.conclusion && job.conclusion !== "success") return "failed";
  if (["queued", "in_progress", "waiting", "pending"].includes(job.status)) return "active";
  return "defined";
}

function renderRemoteJobs() {
  const panel = byId("remote-jobs-panel");
  const container = byId("remote-job-list");
  const jobs = [...remoteJobs.values()];
  panel.hidden = jobs.length === 0;
  const cards = jobs.map((job) => {
    const card = document.createElement("section");
    card.className = "remote-job";
    const heading = document.createElement("div");
    heading.className = "remote-job-heading";
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = job.preview_id || job.remote_job_id;
    const target = document.createElement("small");
    target.textContent = `${job.repository || "GitHub"} · ${job.ref || "—"}`;
    identity.append(title, target);
    const status = document.createElement("span");
    status.className = `job-status ${remoteJobStatusClass(job)}`;
    status.textContent = job.state || job.status || "unknown";
    heading.append(identity, status);
    const detail = document.createElement("code");
    detail.textContent = `${job.remote_job_id} · run ${job.run_id || "pending"}`;
    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (job.run_url) {
      const runLink = document.createElement("a");
      runLink.className = "report-link";
      runLink.href = job.run_url;
      runLink.target = "_blank";
      runLink.rel = "noreferrer";
      runLink.textContent = "Open GitHub run";
      actions.append(runLink);
    }
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "compact-button";
    refresh.textContent = "Refresh";
    refresh.addEventListener("click", () => mutateRemoteJob(job.remote_job_id, "refresh", refresh));
    actions.append(refresh);
    if (job.status === "completed" && job.conclusion === "success" && !job.evidence_available) {
      const download = document.createElement("button");
      download.type = "button";
      download.className = "compact-button";
      download.textContent = "Download + verify";
      download.addEventListener("click", () => mutateRemoteJob(job.remote_job_id, "download", download));
      actions.append(download);
    }
    for (const report of job.reports || []) {
      const link = document.createElement("a");
      link.className = "report-link";
      link.href = report.url;
      link.textContent = `Open ${report.name} report`;
      actions.append(link);
    }
    card.append(heading, detail, actions);
    return card;
  });
  container.replaceChildren(...cards);
}

async function loadRemoteJobs() {
  try {
    const response = await fetch("/api/remote/jobs");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Remote jobs could not be read");
    remoteJobs = new Map((result.jobs || []).map((job) => [job.remote_job_id, job]));
    renderRemoteJobs();
  } catch (error) {
    renderErrors([{path: "remote_jobs", message: error.message}]);
  }
}

async function mutateRemoteJob(remoteJobId, action, button) {
  button.disabled = true;
  const label = button.textContent;
  button.textContent = action === "download" ? "Verifying…" : "Refreshing…";
  try {
    const response = await fetch(`/api/remote/jobs/${encodeURIComponent(remoteJobId)}/${action}`, {
      method: "POST",
      headers: {"X-LTspice-System-Builder": "1"},
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || `Remote ${action} failed`);
    remoteJobs.set(result.remote_job_id, result);
    renderRemoteJobs();
    renderErrors([]);
  } catch (error) {
    renderErrors([{path: `remote_${action}`, message: error.message}]);
    button.disabled = false;
    button.textContent = label;
  }
}

async function dispatchRemoteStudy() {
  if (!recipe || !frozenLaunch || !latestRemotePreview || !remoteAuthReady) return;
  const button = byId("remote-dispatch-button");
  button.disabled = true;
  button.textContent = "Dispatching…";
  try {
    const response = await fetch("/api/remote/dispatch", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        launch_token: frozenLaunch.launch_token,
        confirmed_plan_id: frozenLaunch.plan.plan_id,
        confirmed_run_count: frozenLaunch.execution.total_run_count,
        confirmed_preview_id: latestRemotePreview.preview_id,
        confirmed_preview_sha256: latestRemotePreview.preview_sha256,
        repository: byId("remote-repository").value.trim(),
        ref: byId("remote-ref").value.trim(),
        recipe,
        acknowledged: byId("remote-acknowledgement").checked,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Remote dispatch failed");
    remoteJobs.set(result.job.remote_job_id, result.job);
    renderRemoteJobs();
    byId("security-label").textContent = "Remote active";
    byId("remote-mode-status").className = "status-pill valid";
    byId("remote-mode-status").textContent = "Submitted";
    byId("remote-acknowledgement").disabled = true;
    byId("remote-auth-button").disabled = true;
    button.textContent = "Plan dispatched";
    renderErrors([]);
  } catch (error) {
    button.textContent = "Dispatch exact plan";
    renderErrors([{path: "remote_dispatch", message: error.message}]);
    updateRemoteDispatchGate();
  }
}

function renderLaunchResult(result) {
  for (const experiment of result.experiments) {
    trackedJobs.set(experiment.experiment_id, {
      ...experiment,
      finished_points: 0,
      passed_points: 0,
      failed_points: 0,
      report_available: false,
    });
  }
  renderTrackedJobs();
  scheduleJobPoll(250);
}

async function startStudy() {
  if (!recipe || !frozenLaunch || !byId("execution-acknowledgement").checked) return;
  const button = byId("start-button");
  button.disabled = true;
  button.textContent = "Queuing…";
  try {
    const response = await fetch("/api/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        launch_token: frozenLaunch.launch_token,
        recipe,
        confirmed_run_count: frozenLaunch.execution.total_run_count,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Study could not be started");
    renderLaunchResult(result);
    byId("execution-acknowledgement").disabled = true;
    button.textContent = "Study queued";
    await loadHistory();
  } catch (error) {
    renderErrors([{path: "execution", message: error.message}]);
    button.disabled = false;
    button.textContent = "Start local study";
  }
}

function renderProjectsError(message) {
  const container = byId("projects-errors");
  if (!message) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  container.textContent = message;
  container.hidden = false;
}

async function openProject(project) {
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(project.slug)}/recipe`);
    const loaded = await response.json();
    if (!response.ok) throw new Error(loaded.error?.message || "Recipe could not be loaded");
    if (project.kind === "optimization") {
      optimizationRecipe = loaded;
      optimizationDisplayUnits = new WeakMap();
      renderOptimizationEditors();
      await previewOptimization();
      showView("optimization");
    } else {
      recipe = loaded;
      variableDisplayUnits = new WeakMap();
      cornerDisplayUnits = new WeakMap();
      invalidateFrozenPlan();
      populateRecipeControls();
      await loadNetlistFiles();
      await preview();
      showView("definition");
    }
  } catch (error) {
    renderProjectsError(`${project.name}: ${error.message}`);
  }
}

function renderProjects(projects) {
  const grid = byId("projects-grid");
  byId("projects-empty").hidden = projects.length > 0;
  grid.replaceChildren(
    ...projects.map((project) => {
      const card = document.createElement("article");
      card.className = project.valid ? "project-card" : "project-card invalid";

      const heading = document.createElement("div");
      heading.className = "project-card-heading";
      const name = document.createElement("strong");
      name.textContent = project.name;
      heading.append(name);
      if (project.kind) {
        const badge = document.createElement("span");
        badge.className = "kind-badge";
        badge.textContent = project.kind;
        heading.append(badge);
      }

      const description = document.createElement("p");
      description.textContent = project.valid
        ? project.description || "No description."
        : "This project's recipe file could not be read.";

      const path = document.createElement("code");
      path.textContent = project.path;

      const buttonRow = document.createElement("div");
      buttonRow.className = "button-row";
      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "secondary-button";
      openButton.textContent = "Open";
      openButton.disabled = !project.valid;
      openButton.addEventListener("click", () => openProject(project));
      buttonRow.append(openButton);

      card.append(heading, description, path, buttonRow);
      return card;
    }),
  );
}

async function loadProjects() {
  const button = byId("refresh-projects");
  button.disabled = true;
  try {
    const response = await fetch("/api/projects");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Projects could not be read");
    renderProjectsError(null);
    renderProjects(result.projects);
  } catch (error) {
    renderProjectsError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadInitialState() {
  const [sessionResponse, recipeResponse] = await Promise.all([
    fetch("/api/session"),
    fetch("/api/examples/mixed-signal-daq"),
  ]);
  if (!sessionResponse.ok || !recipeResponse.ok) throw new Error("Local session could not be established");
  const session = await sessionResponse.json();
  recipe = await recipeResponse.json();
  variableDisplayUnits = new WeakMap();
  cornerDisplayUnits = new WeakMap();
  invalidateFrozenPlan();
  byId("workspace").textContent = session.workspace;
  byId("workspace").title = session.workspace;
  byId("projects-workspace").textContent = session.workspace;
  populateRecipeControls();
  await Promise.all([loadSchematicFiles(), loadNetlistFiles()]);
  await preview();
  await Promise.all([loadHistory(), loadRemoteJobs(), loadProjects()]);
}

byId("preview-button").addEventListener("click", preview);
byId("freeze-button").addEventListener("click", freezePlan);
byId("remote-preview-button").addEventListener("click", previewRemoteExecution);
byId("remote-auth-button").addEventListener("click", checkRemoteAuth);
byId("remote-dispatch-button").addEventListener("click", dispatchRemoteStudy);
byId("remote-acknowledgement").addEventListener("change", updateRemoteDispatchGate);
for (const id of ["remote-repository", "remote-ref"]) {
  byId(id).addEventListener("input", () => {
    latestRemotePreview = null;
    remoteAuthReady = false;
    byId("remote-preview-result").hidden = true;
    byId("remote-dispatch-button").disabled = true;
  });
}
byId("execution-acknowledgement").addEventListener("change", () => {
  byId("start-button").disabled = !byId("execution-acknowledgement").checked;
});
byId("start-button").addEventListener("click", startStudy);
byId("capture-schematic").addEventListener("click", captureSchematic);
byId("refresh-schematic").addEventListener("click", () => {
  if (!recipe) return;
  const context = schematicContext();
  const selected = byId("schematic-image-path").value.trim();
  if (selected) context.schematic_path = selected;
  else delete context.schematic_path;
  showSchematicImage(true);
  schedulePreview();
});
byId("schematic-source-path").addEventListener("input", () => {
  if (!recipe) return;
  const context = schematicContext();
  const value = byId("schematic-source-path").value;
  if (value) context.schematic_source_path = value;
  else delete context.schematic_source_path;
  schedulePreview();
});
byId("schematic-image-path").addEventListener("input", () => {
  if (!recipe) return;
  const context = schematicContext();
  const value = byId("schematic-image-path").value;
  if (value) context.schematic_path = value;
  else delete context.schematic_path;
  schedulePreview();
});
for (const id of ["sample-count", "seed", "sampling-method"]) {
  byId(id).addEventListener("input", schedulePreview);
}
byId("add-variable").addEventListener("click", () => {
  if (!recipe) return;
  const variables = recipe.plan.variables || (recipe.plan.variables = []);
  const names = new Set(variables.map((variable) => variable.name));
  let suffix = variables.length + 1;
  while (names.has(`PARAM${suffix}`)) suffix += 1;
  variables.push({
    name: `PARAM${suffix}`,
    distribution: "gaussian",
    nominal: 1,
    sigma: 0.01,
    minimum: 0.95,
    maximum: 1.05,
    unit: "",
  });
  populateVariables();
  populateCorrelations();
  schedulePreview();
});
byId("add-correlation").addEventListener("click", () => {
  if (!recipe) return;
  const groups = recipe.plan.correlations || (recipe.plan.correlations = []);
  const used = new Set(groups.flatMap((group) => group.variables || []));
  const available = (recipe.plan.variables || [])
    .filter((variable) => variable.distribution === "gaussian" && !used.has(variable.name))
    .slice(0, 2)
    .map((variable) => variable.name);
  if (available.length < 2) return;
  groups.push({variables: available, matrix: [[1, 0], [0, 1]]});
  populateCorrelations();
  schedulePreview();
});
byId("add-corner").addEventListener("click", () => {
  if (!recipe) return;
  const axes = recipe.plan.corner_axes || (recipe.plan.corner_axes = []);
  const names = new Set(axes.map((axis) => axis.name));
  let suffix = axes.length + 1;
  while (names.has(`corner_${suffix}`)) suffix += 1;
  axes.push({
    name: `corner_${suffix}`,
    parameter: `CORNER${suffix}`,
    unit: "",
    values: [{name: "nominal", value: 1}],
  });
  populateCorners();
  schedulePreview();
});
byId("add-experiment").addEventListener("click", () => {
  if (!recipe) return;
  const experiments = recipe.experiments || (recipe.experiments = []);
  const names = new Set(experiments.map((experiment) => experiment.name));
  let suffix = experiments.length + 1;
  while (names.has(`experiment_${suffix}`)) suffix += 1;
  const defaultNetlist = netlistFiles[0] || "";
  experiments.push({
    name: `experiment_${suffix}`,
    netlist_path: defaultNetlist,
    filename: defaultNetlist.split("/").pop() || "",
    waveform_analyses: [
      {
        name: "response",
        variable: "V(out)",
        requirements: [{metric: "maximum", operator: "<=", target: 0}],
      },
    ],
  });
  populateExperiments();
  schedulePreview();
});
byId("recipe-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    recipe = JSON.parse(await file.text());
    variableDisplayUnits = new WeakMap();
    cornerDisplayUnits = new WeakMap();
    invalidateFrozenPlan();
    populateRecipeControls();
    await preview();
  } catch (error) {
    renderPreview({valid: false, errors: [{path: "$", message: `Could not load recipe: ${error.message}`}]});
  }
  event.target.value = "";
});
byId("save-button").addEventListener("click", () => {
  if (!recipe) return;
  updateRecipeFromControls();
  const blob = new Blob([`${JSON.stringify(recipe, null, 2)}\n`], {type: "application/json"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "mixed-signal-daq.ltstudy.json";
  link.click();
  URL.revokeObjectURL(link.href);
});
byId("refresh-history").addEventListener("click", loadHistory);
byId("refresh-projects").addEventListener("click", loadProjects);
byId("refresh-netlists").addEventListener("click", async () => {
  const button = byId("refresh-netlists");
  button.disabled = true;
  try {
    await loadNetlistFiles();
    schedulePreview();
  } catch (error) {
    renderScopedErrors([{path: "experiments", message: error.message}]);
  } finally {
    button.disabled = false;
  }
});
byId("new-project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("new-project-name");
  const name = input.value.trim();
  if (!name) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    const response = await fetch("/api/projects", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({name}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Project could not be created");
    renderProjectsError(null);
    input.value = "";
    await loadProjects();
    await openProject(result.project);
  } catch (error) {
    renderProjectsError(error.message);
  } finally {
    button.disabled = false;
  }
});
byId("theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(theme);
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch (_) {
    // Theme switching still works for this page when storage is disabled.
  }
});

loadInitialState().catch((error) => {
  renderPreview({valid: false, errors: [{path: "$", message: error.message}]});
});
