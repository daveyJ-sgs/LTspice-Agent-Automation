"use strict";

let recipe = null;

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

function engineering(value, unit) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const absolute = Math.abs(number);
  const scales = [
    [1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""],
    [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"],
  ];
  const selected = scales.find(([scale]) => absolute >= scale * 0.999) || [1e-12, "p"];
  const scaled = number / selected[0];
  return `${Number(scaled.toPrecision(4))} ${selected[1]}${unit || ""}`.trim();
}

function updateRecipeFromControls() {
  if (!recipe) return;
  recipe.plan.sample_count = Number(byId("sample-count").value);
  recipe.plan.seed = Number(byId("seed").value);
  recipe.plan.sampling_method = byId("sampling-method").value;
}

function populateRecipeControls() {
  byId("study-description").textContent = recipe.description || "Portable LTspice study recipe";
  byId("sample-count").value = recipe.plan.sample_count;
  byId("seed").value = recipe.plan.seed;
  byId("sampling-method").value = recipe.plan.sampling_method || "independent";
  const variables = recipe.plan.variables || [];
  byId("variable-count").textContent = `${variables.length} variables`;
  byId("variables").replaceChildren(...variables.map((variable) => {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    const nominal = document.createElement("td");
    const distribution = document.createElement("td");
    name.textContent = variable.name;
    nominal.textContent = engineering(variable.nominal, variable.unit);
    distribution.textContent = variable.distribution.replaceAll("_", " ");
    row.append(name, nominal, distribution);
    return row;
  }));
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

function renderPreview(result) {
  const status = byId("preview-status");
  if (!result.valid) {
    status.className = "status-pill invalid";
    status.textContent = "Needs attention";
    byId("preview-title").textContent = "Definition is not valid";
    renderErrors(result.errors);
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
    renderPreview(result);
  } catch (error) {
    renderPreview({valid: false, errors: [{path: "$", message: error.message}]});
  } finally {
    button.disabled = false;
    button.textContent = "Preview resolved plan";
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
