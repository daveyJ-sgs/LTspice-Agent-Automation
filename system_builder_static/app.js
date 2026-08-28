"use strict";

let recipe = null;

const byId = (id) => document.getElementById(id);

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

loadInitialState().catch((error) => {
  renderPreview({valid: false, errors: [{path: "$", message: error.message}]});
});
