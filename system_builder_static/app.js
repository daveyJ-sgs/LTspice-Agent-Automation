"use strict";

let recipe = null;
let previewTimer = null;
let previewSequence = 0;

const byId = (id) => document.getElementById(id);
const THEME_KEY = "ltspice-system-builder-theme";

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

function schedulePreview() {
  if (!recipe) return;
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

function populateVariables() {
  const variables = recipe.plan.variables || [];
  byId("variable-count").textContent = `${variables.length} variables`;
  const rows = variables.map((variable, index) => {
    const base = `plan.variables[${index}]`;
    const row = document.createElement("tr");
    row.dataset.path = base;
    const name = fieldInput(variable.name, `${base}.name`, "variable-name");
    setRecipeField(name, variable, "name");
    const distribution = selectInput(variable.distribution, [
      ["gaussian", "Gaussian"],
      ["uniform", "Uniform"],
    ], `${base}.distribution`, "distribution");
    distribution.addEventListener("change", () => {
      variable.distribution = distribution.value;
      if (distribution.value === "uniform") {
        delete variable.sigma;
      } else if (!(Number(variable.sigma) > 0)) {
        variable.sigma = Math.abs(Number(variable.maximum) - Number(variable.minimum)) / 6 || 1;
      }
      populateVariables();
      schedulePreview();
    });
    const nominal = fieldInput(variable.nominal, `${base}.nominal`);
    setRecipeField(nominal, variable, "nominal", true);
    const tolerance = fieldInput(variable.sigma, `${base}.sigma`);
    tolerance.placeholder = distribution.value === "gaussian" ? "σ" : "n/a";
    tolerance.disabled = distribution.value !== "gaussian";
    if (!tolerance.disabled) setRecipeField(tolerance, variable, "sigma", true);
    const minimum = fieldInput(variable.minimum, `${base}.minimum`);
    setRecipeField(minimum, variable, "minimum", true);
    const maximum = fieldInput(variable.maximum, `${base}.maximum`);
    setRecipeField(maximum, variable, "maximum", true);
    const unit = fieldInput(variable.unit, `${base}.unit`, "unit");
    setRecipeField(unit, variable, "unit");
    for (const control of [name, distribution, nominal, tolerance, minimum, maximum, unit]) {
      const cell = document.createElement("td");
      cell.append(control);
      row.append(cell);
    }
    const remove = document.createElement("td");
    remove.className = "remove-cell";
    remove.append(removeButton(`Remove variable ${variable.name || index + 1}`, () => {
      variables.splice(index, 1);
      populateVariables();
      schedulePreview();
    }));
    row.append(remove);
    return row;
  });
  byId("variables").replaceChildren(...rows);
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
    for (const [key, label] of [["name", "Axis name"], ["parameter", "Netlist parameter"], ["unit", "Unit"]]) {
      const wrapper = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = label;
      const input = fieldInput(axis[key], `${base}.${key}`);
      setRecipeField(input, axis, key);
      wrapper.append(caption, input);
      fields.append(wrapper);
    }
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
      const value = fieldInput(entry.value, `${valueBase}.value`);
      value.placeholder = "Value";
      setRecipeField(value, entry, "value", true);
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

function populateRequirements() {
  let count = 0;
  const cards = [];
  for (const [experimentIndex, experiment] of (recipe.experiments || []).entries()) {
    for (const [analysisIndex, analysis] of (experiment.waveform_analyses || []).entries()) {
      const base = `experiments[${experimentIndex}].waveform_analyses[${analysisIndex}]`;
      const card = document.createElement("section");
      card.className = "editor-card";
      card.dataset.path = base;
      const heading = document.createElement("div");
      heading.className = "editor-card-heading";
      const title = document.createElement("strong");
      title.textContent = analysis.name;
      const meta = document.createElement("span");
      meta.className = "analysis-meta";
      meta.textContent = `${experiment.name.toUpperCase()} · ${analysis.variable}`;
      heading.append(title, meta);
      const rows = document.createElement("div");
      rows.className = "requirement-rows";
      for (const [requirementIndex, requirement] of (analysis.requirements || []).entries()) {
        count += 1;
        const requirementBase = `${base}.requirements[${requirementIndex}]`;
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
          populateRequirements();
          schedulePreview();
        }));
        rows.append(row);
      }
      const add = document.createElement("button");
      add.type = "button";
      add.className = "compact-button";
      add.textContent = "+ Requirement";
      add.addEventListener("click", () => {
        analysis.requirements.push({metric: "maximum", operator: "<=", target: 0});
        populateRequirements();
        schedulePreview();
      });
      rows.append(add);
      card.append(heading, rows);
      cards.push(card);
    }
  }
  byId("requirement-count").textContent = `${count} requirements`;
  byId("requirements").replaceChildren(...(cards.length ? cards : [emptyEditor("No waveform analyses defined.")]));
}

function emptyEditor(message) {
  const empty = document.createElement("p");
  empty.className = "editor-empty";
  empty.textContent = message;
  return empty;
}

function populateRecipeControls() {
  byId("study-description").textContent = recipe.description || "Portable LTspice study recipe";
  byId("sample-count").value = recipe.plan.sample_count;
  byId("seed").value = recipe.plan.seed;
  byId("sampling-method").value = recipe.plan.sampling_method || "independent";
  populateVariables();
  populateCorners();
  populateRequirements();
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
    ["variable-errors", ["plan.variables", "plan.correlations"]],
    ["corner-errors", ["plan.corner_axes"]],
    ["requirement-errors", ["experiments"]],
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
  renderErrors([]);
  renderScopedErrors([]);
  byId("experiments").replaceChildren(...result.experiments.map((experiment) => {
    const card = document.createElement("div");
    card.className = "experiment";
    const icon = document.createElement("span");
    icon.className = "experiment-icon";
    icon.textContent = experiment.name === "ac" ? "AC" : "TR";
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    const detail = document.createElement("small");
    title.textContent = experiment.name === "ac" ? "Frequency response" : "Acquisition transient";
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

    const id = document.createElement("code");
    id.textContent = job.experiment_id;
    row.append(top, progress, bottom, id);
    return row;
  });
  byId("job-history").replaceChildren(...(jobs.length ? jobs : [emptyHistory("No durable experiments found.")]));

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

async function loadHistory() {
  const button = byId("refresh-history");
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    const response = await fetch("/api/history?limit=12");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "History could not be read");
    renderHistory(result);
  } catch (error) {
    renderHistoryErrors([{path: "workspace", message: error.message}]);
  } finally {
    button.disabled = false;
    button.textContent = "Refresh status";
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

async function loadInitialState() {
  const [sessionResponse, recipeResponse] = await Promise.all([
    fetch("/api/session"),
    fetch("/api/examples/mixed-signal-daq"),
  ]);
  if (!sessionResponse.ok || !recipeResponse.ok) throw new Error("Local session could not be established");
  const session = await sessionResponse.json();
  recipe = await recipeResponse.json();
  byId("workspace").textContent = session.workspace;
  byId("workspace").title = session.workspace;
  populateRecipeControls();
  await preview();
  await loadHistory();
}

byId("preview-button").addEventListener("click", preview);
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
byId("recipe-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    recipe = JSON.parse(await file.text());
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
