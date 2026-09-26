"use strict";

let recipe = null;
let previewTimer = null;
let previewSequence = 0;
let latestPreview = null;
let frozenLaunch = null;
// Bumped by every invalidation, so a /api/freeze response that arrives after
// the recipe changed is dropped instead of re-arming Start for a plan that no
// longer matches the screen.
let freezeSequence = 0;
let latestRemotePreview = null;
let remoteAuthReady = false;
let remoteJobs = new Map();
let trackedJobs = new Map();
let jobPollTimer = null;
let variableDisplayUnits = new WeakMap();
let cornerDisplayUnits = new WeakMap();
let netlistFiles = [];
let schematicSourceFiles = []; // workspace-relative .asc paths, for bare-filename resolution
let currentStudyProjectPath = null; // workspace-relative folder of the open Study project, if any
let currentStudyProjectSlug = null; // the open Study project's slug, if any -- Save recipe writes here instead of downloading
let studyDirty = false; // true once the loaded recipe has edits Save hasn't persisted yet

function markDirty(statusId, flagSetter) {
  flagSetter(true);
  const status = byId(statusId);
  status.textContent = "Unsaved changes";
  status.classList.remove("is-error");
  status.classList.add("unsaved");
}

function markClean(statusId, flagSetter) {
  flagSetter(false);
  const status = byId(statusId);
  status.textContent = "";
  status.classList.remove("unsaved", "is-error");
}

// Study and Optimization are independent documents -- each project is only
// ever one kind or the other, so each tracks its own project association
// (and its own Save button label) separately. Setting one must never touch
// the other's, or opening a Study project relabels Optimization's Save
// button to "Save to project" too, even though optimizationRecipe is still
// whatever unrelated thing was loaded there.
function setCurrentStudyProject(slug, path) {
  currentStudyProjectSlug = slug;
  currentStudyProjectPath = path;
  byId("save-button").textContent = slug ? "Save to project" : "Save recipe";
}

const byId = (id) => document.getElementById(id);
const THEME_KEY = "ltspice-system-builder-theme";
const UNIT_CHOICES = {
  F: [["pF", "pF", 1e-12], ["nF", "nF", 1e-9], ["uF", "µF", 1e-6]],
  ohm: [["ohm", "Ω", 1], ["kohm", "kΩ", 1e3], ["Mohm", "MΩ", 1e6]],
};

const THEMES = ["dark", "light", "wiregrid"]; // "Solder Mask", "Copper Print", "Wire & Grid" -- no OS-auto option, pick one explicitly

function preferredTheme() {
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (THEMES.includes(saved)) return saved;
  } catch (_) {
    // Falls back to the default theme if browser storage is disabled.
  }
  return "dark";
}

function applyTheme(theme) {
  const resolved = THEMES.includes(theme) ? theme : "dark";
  document.documentElement.dataset.theme = resolved;
  byId("theme-select").value = resolved;
}

applyTheme(preferredTheme());

// Real view routing: exactly one top-level section is visible at a time,
// switched by clicking a nav link or a data-view control anywhere on the
// page, with the current view reflected in the URL hash so back/forward
// and reload land where you left off. This replaces the previous
// anchor-scroll navigation, where every section lived in the DOM at once
// and "switching" meant scrolling.
const VIEWS = ["dashboard", "projects", "definition", "optimization", "qualification", "history", "guide", "faq"];
const VIEW_LABELS = {
  dashboard: "Dashboard",
  projects: "Projects",
  definition: "Study setup",
  optimization: "Optimization",
  qualification: "Qualification",
  history: "Workspace",
  guide: "Guide",
  faq: "FAQ",
};

function confirmDiscard(dirty, message) {
  return !dirty || window.confirm(message);
}

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
  setNavDrawer(false);
  if (window.location.hash.slice(1) !== view) {
    window.history.pushState(null, "", `#${view}`);
  }
  window.scrollTo({top: 0});
}

// Below the narrow breakpoint the sidebar is a top bar and its navigation a
// drawer; on wider screens the toggle is hidden and the class is inert.
function setNavDrawer(open) {
  const toggle = byId("nav-toggle");
  if (!toggle) return;
  toggle.setAttribute("aria-expanded", String(open));
  document.querySelector(".app-sidebar").classList.toggle("nav-open", open);
}

byId("nav-toggle").addEventListener("click", () => {
  setNavDrawer(byId("nav-toggle").getAttribute("aria-expanded") !== "true");
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && byId("nav-toggle").getAttribute("aria-expanded") === "true") {
    setNavDrawer(false);
    byId("nav-toggle").focus();
  }
});

function routeFromHash() {
  showView((window.location.hash || "#dashboard").slice(1));
}

window.addEventListener("hashchange", routeFromHash);
window.addEventListener("beforeunload", (event) => {
  if (!studyDirty && !optimizationDirty && !hasUnsavedNetlistEdits()) return;
  event.preventDefault();
  event.returnValue = "";
});
// "Load recipe", "Import netlist" and "Load .ltopt.json" are real buttons
// (focusable, announced as buttons) that open their hidden file input.
document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-file-input]");
  if (opener) byId(opener.dataset.fileInput)?.click();
});
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
  // The GUI never authored this block before, so a recipe loaded from an
  // older file may not carry one.
  const execution = recipe.execution || (recipe.execution = {});
  execution.max_concurrency = numericValue(byId("max-concurrency").value);
  execution.reuse_cache = byId("reuse-cache").checked;
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

// Accessible names for generated controls. data-path stays the machine key
// (validation errors are matched against it); screen readers get a phrase
// like "Experiment 1, analysis 1, requirement 2: target" instead.
const PATH_COLLECTIONS = {
  variables: "Variable",
  correlations: "Correlation group",
  corner_axes: "Corner axis",
  values: "Value",
  weights: "Weight",
  experiments: "Experiment",
  waveform_analyses: "Analysis",
  requirements: "Requirement",
  matrix: "Row",
};

function humanizeKey(key) {
  const words = String(key).replaceAll("_", " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function readableLabel(path) {
  const parts = [];
  let field = "";
  for (const segment of String(path).split(".")) {
    const match = /^([A-Za-z_]+)((?:\[\d+\])*)$/.exec(segment);
    if (!match) { field = segment; continue; }
    const [, key, indexes] = match;
    const numbers = [...indexes.matchAll(/\[(\d+)\]/g)].map((item) => Number(item[1]) + 1);
    if (key === "plan" || key === "execution" || key === "report_context") continue;
    if (numbers.length) {
      const noun = PATH_COLLECTIONS[key] || humanizeKey(key);
      parts.push(key === "matrix" && numbers.length === 2
        ? `row ${numbers[0]}, column ${numbers[1]}`
        : `${noun} ${numbers.join(".")}`);
    } else {
      field = key;
    }
  }
  const scope = parts.join(", ");
  const name = field ? humanizeKey(field).toLowerCase() : "";
  if (!scope) return humanizeKey(field || path);
  return name ? `${scope}: ${name}` : scope;
}

function fieldInput(value, path, className = "") {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value ?? "";
  input.dataset.path = path;
  input.setAttribute("aria-label", readableLabel(path));
  input.className = className;
  return input;
}

function selectInput(value, choices, path, className = "") {
  const select = document.createElement("select");
  select.dataset.path = path;
  select.setAttribute("aria-label", readableLabel(path));
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
  freezeSequence += 1;
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
  renderTrackedJobs();
  byId("execution-acknowledgement").checked = false;
  byId("execution-acknowledgement").disabled = false;
  byId("start-button").disabled = true;
  byId("start-button").textContent = "Start local study";
}

// A recipe edit: the saved file is now out of date and any frozen plan no
// longer describes what is on screen.
function schedulePreview() {
  if (!recipe) return;
  markDirty("save-status", (v) => { studyDirty = v; });
  invalidateFrozenPlan();
  requestPreview();
}

// Re-resolve without touching the recipe -- after a netlist rescan or save.
// The recipe stays clean, and a frozen plan survives unless the fresh
// preview resolves to a different plan (renderPreview checks that).
function requestPreview() {
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

// Files inside the currently open project show as a bare filename -- the
// project is already established by context, so the prefix is just noise.
// Anything outside it (or when no project is open) keeps the full
// workspace-relative path so it's still unambiguous.
function schematicSourceDisplayName(path) {
  if (currentStudyProjectPath && path.startsWith(`${currentStudyProjectPath}/`)) {
    return path.slice(currentStudyProjectPath.length + 1);
  }
  return path;
}

function populateSchematicControls() {
  const context = schematicContext();
  byId("schematic-source-path").value = schematicSourceDisplayName(context.schematic_source_path || "");
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
  schematicSourceFiles = result.sources || [];
  populate("schematic-source-files", schematicSourceFiles.map(schematicSourceDisplayName));
  populate("schematic-image-files", result.images || []);
}

// Netlist text, keyed by workspace-relative path, in two separate maps: what
// is on disk (a cache, dropped whenever the file list is rescanned) and what
// the user has typed but not saved. Keeping them apart means a rescan,
// import, or structural re-render never throws away an unsaved edit, and the
// page can tell when the editor and the file Simulate once runs disagree.
const netlistDiskText = new Map();
const netlistEdits = new Map();

function netlistText(path) {
  return netlistEdits.has(path) ? netlistEdits.get(path) : netlistDiskText.get(path);
}

function hasUnsavedNetlistEdits() {
  return netlistEdits.size > 0;
}

function recordNetlistEdit(path, text) {
  if (!path) return;
  if (netlistDiskText.has(path) && netlistDiskText.get(path) === text) netlistEdits.delete(path);
  else netlistEdits.set(path, text);
}

// Served from the measurement registries by /api/metrics, so the requirement
// form offers exactly the parameters each metric actually reads instead of a
// table kept in step by hand.
let metricSchema = new Map();

async function loadMetricSchema() {
  const response = await fetch("/api/metrics");
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || "Metric schema could not be read");
  metricSchema = new Map((result.metrics || []).map((metric) => [metric.name, metric]));
}

function metricDefinition(name) {
  return metricSchema.get(name) || null;
}

function metricParameters(name) {
  return metricDefinition(name)?.parameters || [];
}

// SPICE magnitude suffixes. "meg" and "mil" are listed before "m" because a
// prefix match would otherwise read 1Meg as 1 milli. Decimal scales are
// exponents added to the literal's own exponent, so 10u parses as the literal
// 10e-6 (1e-5) rather than 10 * 1e-6 (9.999999999999999e-6).
const SPICE_SCALES = [
  ["meg", 6], ["mil", null], ["t", 12], ["g", 9], ["k", 3],
  ["m", -3], ["u", -6], ["n", -9], ["p", -12], ["f", -15],
];

function spiceNumber(token) {
  const match = /^([+-]?(?:\d+\.?\d*|\.\d+))(?:e([+-]?\d+))?([a-zµμ]*)$/i.exec(String(token ?? "").trim());
  if (!match) return NaN;
  const exponent = match[2] === undefined ? 0 : Number(match[2]);
  // Micro may be typed as the micro sign (U+00B5) or Greek mu (U+03BC).
  const suffix = match[3].toLowerCase().replace(/[µμ]/g, "u");
  const scale = SPICE_SCALES.find(([name]) => suffix.startsWith(name));
  if (scale && scale[1] === null) return Number(`${match[1]}e${exponent}`) * 25.4e-6;
  return Number(`${match[1]}e${exponent + (scale ? scale[1] : 0)}`);
}

// The swept range a frequency-valued requirement parameter has to land inside.
// Returns null when the directive is missing or parameterised ({FMAX}), in
// which case the field simply goes unhinted and the server still validates.
function acSweepRange(netlistText) {
  const directive = /^[ \t]*\.ac[ \t]+(\S+)[ \t]+(.+)$/im.exec(netlistText || "");
  if (!directive) return null;
  const spacing = directive[1].toLowerCase();
  const fields = directive[2].trim().split(/\s+/);
  if (spacing === "list") {
    const points = fields.map(spiceNumber).filter((value) => Number.isFinite(value) && value > 0);
    if (points.length === 0) return null;
    return {spacing, start: Math.min(...points), stop: Math.max(...points)};
  }
  if (!["dec", "oct", "lin"].includes(spacing) || fields.length < 3) return null;
  const start = spiceNumber(fields[1]);
  const stop = spiceNumber(fields[2]);
  if (!Number.isFinite(start) || !Number.isFinite(stop) || start <= 0 || stop < start) return null;
  return {spacing, points: spiceNumber(fields[0]), start, stop};
}

// Simulate once runs a study template at its nominal point: each variable's
// {NAME} placeholder takes the nominal the editor holds, in base units.
function nominalParameters() {
  const parameters = {};
  for (const variable of (recipe?.plan?.variables || [])) {
    if (!variable.name) continue;
    const nominal = variable.nominal;
    if ((typeof nominal === "number" && Number.isFinite(nominal))
      || (typeof nominal === "string" && nominal.trim())) {
      parameters[variable.name] = nominal;
    }
  }
  return parameters;
}

function formatHertz(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? "—");
  const magnitude = Math.abs(number);
  const [factor, label] = [[1e9, "GHz"], [1e6, "MHz"], [1e3, "kHz"], [1, "Hz"]]
    .find(([candidate]) => magnitude >= candidate) || [1, "Hz"];
  return `${Number((number / factor).toPrecision(4))} ${label}`;
}

function formatDefault(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "number" ? String(Number(value.toPrecision(6))) : String(value);
}

// Netlist text is fetched for the sweep-range hint even when this experiment
// has no open netlist editor. Re-renders once on arrival; a failed read just
// leaves the hint out rather than retrying in a loop.
const netlistTextRequests = new Set();

function experimentNetlistText(experiment) {
  const path = experiment?.netlist_path;
  if (!path) return null;
  const known = netlistText(path);
  if (known !== undefined) return known;
  requestNetlistText(path);
  return null;
}

async function requestNetlistText(path) {
  if (netlistTextRequests.has(path)) return;
  netlistTextRequests.add(path);
  try {
    const response = await fetch(`/api/recipe/netlist?path=${encodeURIComponent(path)}`);
    const result = await response.json();
    if (!response.ok) return;
    netlistDiskText.set(path, result.content);
    if (recipe) populateExperiments();
  } catch (_) {
    // The hint is an aid; the backend still range-checks every frequency.
  } finally {
    netlistTextRequests.delete(path);
  }
}

async function loadNetlistFiles() {
  const response = await fetch("/api/recipe/netlists");
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || "Netlist files could not be listed");
  netlistFiles = result.files || [];
  // Only the disk cache: a file may have changed on disk (re-exported from
  // LTspice), but text typed into an editor stays until it is saved.
  netlistDiskText.clear();
  if (recipe) populateExperiments();
}

function buildNetlistEditor(experiment) {
  const container = document.createElement("div");
  container.className = "netlist-editor";
  const path = experiment.netlist_path;

  const toolbar = document.createElement("div");
  toolbar.className = "netlist-editor-toolbar";
  const label = document.createElement("span");
  label.className = "muted-copy";
  label.textContent = "Insert variable:";
  toolbar.append(label);

  const textarea = document.createElement("textarea");
  textarea.className = "netlist-textarea";
  textarea.spellcheck = false;
  textarea.rows = 14;
  textarea.disabled = true;
  textarea.placeholder = "Pick a netlist above to view and edit its text here.";
  textarea.setAttribute(
    "aria-label",
    path ? `Netlist text for ${path.split("/").pop()}` : "Netlist text",
  );

  const status = document.createElement("span");
  status.className = "muted-copy netlist-editor-status";
  status.setAttribute("aria-live", "polite");

  function setStatus(message, tone = "") {
    status.textContent = message;
    status.classList.toggle("unsaved", tone === "unsaved");
    status.classList.toggle("is-error", tone === "error");
  }

  function showDirtyState() {
    if (netlistEdits.has(path)) {
      setStatus("Unsaved edits — save the netlist before simulating or starting a study.", "unsaved");
    } else if (status.classList.contains("unsaved")) {
      setStatus("");
    }
  }

  function edited() {
    recordNetlistEdit(path, textarea.value);
    showDirtyState();
  }

  for (const variable of (recipe.plan.variables || [])) {
    if (!variable.name) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "compact-button";
    button.textContent = `{${variable.name}}`;
    button.title = `Insert {${variable.name}} at the cursor`;
    button.addEventListener("click", () => {
      const insertText = `{${variable.name}}`;
      const start = textarea.selectionStart ?? textarea.value.length;
      const end = textarea.selectionEnd ?? textarea.value.length;
      textarea.value = textarea.value.slice(0, start) + insertText + textarea.value.slice(end);
      edited();
      const cursor = start + insertText.length;
      textarea.focus();
      textarea.setSelectionRange(cursor, cursor);
    });
    toolbar.append(button);
  }

  textarea.addEventListener("input", edited);

  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.className = "primary-button";
  saveButton.textContent = "Save netlist";
  saveButton.disabled = true;

  async function saveNetlist() {
    const text = textarea.value;
    const response = await fetch(`/api/recipe/netlist?path=${encodeURIComponent(path)}`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({content: text}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Netlist could not be saved");
    netlistDiskText.set(path, text);
    recordNetlistEdit(path, textarea.value);
  }

  saveButton.addEventListener("click", async () => {
    if (!path) return;
    saveButton.disabled = true;
    setStatus("Saving…");
    try {
      await saveNetlist();
      setStatus("Saved.");
      showDirtyState();
      requestPreview();
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      saveButton.disabled = false;
    }
  });

  const runButton = document.createElement("button");
  runButton.type = "button";
  runButton.className = "compact-button";
  runButton.textContent = "Simulate once";
  runButton.title = "Run this deck through LTspice now, without defining a study";
  runButton.disabled = true;
  runButton.dataset.needsLtspice = "true";
  runButton.addEventListener("click", async () => {
    if (!path) return;
    // Simulate once runs the file on disk, so an unsaved edit would silently
    // not be what gets simulated.
    if (netlistEdits.has(path)) {
      if (!window.confirm("This netlist has unsaved edits, and Simulate once runs the saved file. Save your edits and simulate?")) return;
      runButton.disabled = true;
      setStatus("Saving…");
      try {
        await saveNetlist();
        requestPreview();
      } catch (error) {
        setStatus(error.message, "error");
        runButton.disabled = false;
        return;
      }
    }
    runButton.disabled = true;
    setStatus("Simulating…");
    try {
      const response = await fetch("/api/netlist/run", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-LTspice-System-Builder": "1",
        },
        body: JSON.stringify({netlist_path: path, parameters: nominalParameters()}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || "Simulation failed");
      const captures = result.captures || [];
      setStatus(`${result.status} · ${captures.length} capture${captures.length === 1 ? "" : "s"}`);
      if (captures.length) {
        showView("history");
        openWaveforms(result.run_id);
      }
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      runButton.disabled = false;
      applyLtspiceGate();
    }
  });

  const buttonRow = document.createElement("div");
  buttonRow.className = "button-row";
  buttonRow.append(saveButton, runButton, status, ltspiceGateNote());

  container.append(toolbar, textarea, buttonRow);

  const enable = (text) => {
    textarea.value = text;
    textarea.disabled = false;
    saveButton.disabled = false;
    runButton.disabled = false;
    applyLtspiceGate();
    showDirtyState();
  };

  (async () => {
    if (!path) return;
    const known = netlistText(path);
    if (known !== undefined) {
      enable(known);
      return;
    }
    setStatus("Loading…");
    try {
      const response = await fetch(`/api/recipe/netlist?path=${encodeURIComponent(path)}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || "Netlist could not be loaded");
      netlistDiskText.set(path, result.content);
      setStatus("");
      enable(netlistText(path));
    } catch (error) {
      setStatus(error.message, "error");
    }
  })();

  return container;
}

function commonDirectoryPrefix(paths) {
  if (paths.length === 0) return "";
  const segmented = paths.map((path) => path.split("/"));
  let commonLength = segmented[0].length - 1;
  for (const segments of segmented.slice(1)) {
    let i = 0;
    while (i < commonLength && i < segments.length - 1 && segments[i] === segmented[0][i]) i++;
    commonLength = i;
  }
  return commonLength > 0 ? segmented[0].slice(0, commonLength).join("/") + "/" : "";
}

function netlistSelect(experiment, path) {
  const value = experiment.netlist_path || "";
  const prefix = commonDirectoryPrefix(netlistFiles);
  const choices = netlistFiles.map((file) => [
    file,
    prefix && file.startsWith(prefix) ? file.slice(prefix.length) : file,
  ]);
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

// Lets the field take just "my_circuit.asc" instead of the full
// "project-slug/my_circuit.asc" -- resolved against the currently open
// project first, then against every .asc in the workspace so it still works
// with no project open. Ambiguous bare names (same filename in two project
// folders) are rejected rather than silently guessing.
function resolveSchematicSourcePath(input) {
  if (!input || input.includes("/")) return input;
  const inProject = currentStudyProjectPath ? `${currentStudyProjectPath}/${input}` : null;
  if (inProject && schematicSourceFiles.includes(inProject)) return inProject;
  const matches = schematicSourceFiles.filter((path) => path.split("/").pop() === input);
  if (matches.length === 1) return matches[0];
  if (matches.length > 1) {
    throw new Error(`"${input}" matches more than one file: ${matches.join(", ")}. Use the full path to pick one.`);
  }
  // No match anywhere yet -- fall back to the open project (if any) so the
  // resulting "not found" error at least points at the right folder.
  return inProject || input;
}

async function captureSchematic() {
  if (!recipe) return;
  let sourcePath = byId("schematic-source-path").value.trim();
  const button = byId("capture-schematic");
  renderSchematicErrors();
  if (!sourcePath) {
    renderSchematicErrors([{message: "Select a workspace-relative LTspice .asc file first."}]);
    return;
  }
  try {
    sourcePath = resolveSchematicSourcePath(sourcePath);
  } catch (error) {
    renderSchematicErrors([{message: error.message}]);
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
    byId("schematic-source-path").value = schematicSourceDisplayName(result.source_path);
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
    values.setAttribute("aria-label", `${variable.name || readableLabel(base)} observations`);
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
    let sigmaHint = null;
    if (continuous) {
      const selectedUnit = variableDisplayUnits.get(variable) || defaultDisplayUnit(variable);
      const factor = studyUnitFactor(variable.unit, selectedUnit);
      nominal = scaledFieldInput(variable.nominal, factor, `${base}.nominal`);
      setScaledRecipeField(nominal, variable, "nominal", factor);
      tolerance = scaledFieldInput(variable.sigma, factor, `${base}.sigma`);
      tolerance.placeholder = distribution.value === "gaussian" ? "σ" : "n/a";
      tolerance.disabled = distribution.value !== "gaussian";
      if (!tolerance.disabled) {
        setScaledRecipeField(tolerance, variable, "sigma", factor);
        // σ is one standard deviation in the variable's own unit; show it as
        // the ±% of nominal most datasheets quote, and the 3σ spread.
        sigmaHint = document.createElement("span");
        sigmaHint.className = "field-hint sigma-hint";
        const updateSigmaHint = () => {
          const sigma = Number(variable.sigma);
          const center = Math.abs(Number(variable.nominal));
          const percent = Number.isFinite(sigma) && center > 0 ? 100 * sigma / center : null;
          sigmaHint.textContent = percent === null ? "" : `±${Number(percent.toPrecision(3))}% 1σ`;
          sigmaHint.title = percent === null
            ? ""
            : `One standard deviation is ±${Number(percent.toPrecision(3))}% of nominal; about 99.7% of parts fall within ±${Number((3 * percent).toPrecision(3))}% (3σ).`;
        };
        updateSigmaHint();
        tolerance.addEventListener("input", updateSigmaHint);
        nominal.addEventListener("input", updateSigmaHint);
      }
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
    // Unit sits right after Distribution, beside the values it scales, so it
    // is on screen without scrolling the table sideways.
    for (const control of [name, distribution, unit]) {
      const cell = document.createElement("td");
      cell.append(control);
      row.append(cell);
    }
    for (const control of [nominal, tolerance, minimum, maximum]) {
      if (control) {
        const cell = document.createElement("td");
        cell.append(control);
        if (control === tolerance && sigmaHint) cell.append(sigmaHint);
        row.append(cell);
      } else {
        row.append(textCell());
      }
    }
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
  // The engine rejects corner_aggregate without corner_axes, so the control
  // only exists while there is something to aggregate over.
  if (!axes.length) delete recipe.plan.corner_aggregate;
  const children = cards.length ? [cornerAggregateControl(), ...cards] : [emptyEditor("No operating-corner axes defined.")];
  byId("corners").replaceChildren(...children);
}

function cornerAggregateControl() {
  const wrapper = document.createElement("label");
  wrapper.className = "checkbox-field aggregate-field";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.id = "corner-aggregate";
  box.dataset.path = "plan.corner_aggregate";
  box.checked = recipe.plan.corner_aggregate === true;
  box.addEventListener("change", () => {
    if (box.checked) recipe.plan.corner_aggregate = true;
    else delete recipe.plan.corner_aggregate;
    schedulePreview();
  });
  const caption = document.createElement("span");
  caption.textContent = "Aggregate across corners";
  const hint = document.createElement("span");
  hint.className = "field-hint";
  hint.textContent = "Judge each sampled point against every corner combination together, instead of scoring the corners separately.";
  wrapper.append(box, caption, hint);
  return wrapper;
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
    let netlistEditor = null;
    if (experiments.length > 1) {
      const netlistWrapper = document.createElement("label");
      const netlistCaption = document.createElement("span");
      netlistCaption.textContent = "Netlist (.cir/.net)";
      netlistWrapper.append(netlistCaption, netlistSelect(experiment, `${base}.netlist_path`));
      fields.append(netlistWrapper);
      netlistEditor = buildNetlistEditor(experiment);
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
      const analysisField = ([key, label, placeholder, hint]) => {
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
        if (hint) {
          const note = document.createElement("span");
          note.className = "field-hint";
          note.textContent = hint;
          wrapper.append(note);
        }
        return wrapper;
      };

      for (const field of [
        ["name", "Analysis name", "response"],
        ["variable", "Signal, e.g. V(out)", "V(out)"],
        ["secondary_variable", "Reference signal (optional)", "V(in)"],
        [
          "signal_unit",
          "Signal unit",
          "V",
          "Carried onto every measured value and margin in the report. Blank reports bare numbers.",
        ],
      ]) {
        analysisFields.append(analysisField(field));
      }

      // Overrides for decks that don't follow the usual shape: a non-default
      // independent vector, a unit the axis name doesn't imply, or one of
      // several .raw files in the run directory.
      const overrides = document.createElement("details");
      overrides.className = "analysis-overrides";
      const overridesSummary = document.createElement("summary");
      overridesSummary.textContent = "Vector and axis overrides";
      const overrideFields = document.createElement("div");
      overrideFields.className = "compact-fields";
      for (const field of [
        [
          "axis_variable",
          "Axis vector",
          "time",
          "Defaults to the RAW file's first vector.",
        ],
        [
          "axis_unit",
          "Axis unit",
          "s",
          "Defaults to Hz for a frequency axis, s otherwise.",
        ],
        [
          "raw_filename",
          "RAW file",
          "circuit.raw",
          "Plain file name. Defaults to the one RAW file found in the run.",
        ],
      ]) {
        overrideFields.append(analysisField(field));
      }
      overrides.open = ["axis_variable", "axis_unit", "raw_filename"].some(
        (key) => analysis[key] !== undefined,
      );
      overrides.append(overridesSummary, overrideFields);

      const rows = document.createElement("div");
      rows.className = "requirement-rows";
      for (const [requirementIndex, requirement] of (analysis.requirements || []).entries()) {
        requirementCount += 1;
        rows.append(buildRequirement(
          analysis,
          requirement,
          requirementIndex,
          `${analysisBase}.requirements[${requirementIndex}]`,
          experiment,
        ));
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
      card.append(analysisHeading, analysisFields, overrides, rows);
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

    group.append(heading, fields);
    if (netlistEditor) group.append(netlistEditor);
    group.append(analysisStack, addAnalysis);
    return group;
  });

  byId("requirement-count").textContent = `${requirementCount} requirements`;
  byId("requirements").replaceChildren(...(groups.length ? groups : [emptyEditor("No experiments defined.")]));
  populatePrimaryNetlist(experiments);
}

function populatePrimaryNetlist(experiments) {
  const row = byId("primary-netlist-row");
  const note = byId("primary-netlist-note");
  const editorSlot = byId("primary-netlist-editor-slot");
  if (experiments.length === 1) {
    row.hidden = false;
    note.hidden = true;
    byId("primary-netlist-field").replaceChildren(
      netlistSelect(experiments[0], "experiments[0].netlist_path")
    );
    editorSlot.hidden = false;
    editorSlot.replaceChildren(buildNetlistEditor(experiments[0]));
  } else {
    row.hidden = true;
    editorSlot.hidden = true;
    editorSlot.replaceChildren();
    note.hidden = experiments.length === 0;
  }
}

const REQUIREMENT_CORE_FIELDS = ["metric", "operator", "target"];

// Grouped so the AC metrics a filter study needs are not mixed in with the
// transient ones. Shared with the optimization goal editor.
function metricOptionGroups() {
  return [["frequency", "AC / frequency domain"], ["time", "Time domain"]]
    .map(([domain, label]) => [
      label,
      [...metricSchema.values()].filter((metric) => metric.domain === domain).map((metric) => metric.name),
    ])
    .filter(([, names]) => names.length > 0);
}

function metricSelect(value, path) {
  const select = document.createElement("select");
  select.dataset.path = path;
  select.setAttribute("aria-label", readableLabel(path));
  let matched = false;
  for (const [label, names] of metricOptionGroups()) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const name of names) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      if (name === value) matched = true;
      group.append(option);
    }
    select.append(group);
  }
  if (!matched) {
    const option = document.createElement("option");
    option.value = value ?? "";
    option.textContent = value ? `${value} (loaded)` : "\u2014 Select a metric \u2014";
    select.prepend(option);
  }
  select.value = value ?? "";
  return select;
}

// Parameters are flat sibling keys of metric/operator/target, so the ones the
// previous metric used have to go when the metric changes -- otherwise they
// linger as fields the new metric cannot accept.
// The unit each metric's measured value comes out in, which is the unit its
// target is compared in. Mirrors the units the waveform_metrics and
// frequency_domain_metrics handlers return; "signal" and "axis" resolve
// against the analysis (signal_unit / axis_unit).
const METRIC_RESULT_UNITS = {
  minimum: "signal", maximum: "signal", mean: "signal", rms: "signal",
  peak_to_peak: "signal", ripple: "signal", monotonicity: "signal", spectral_peak: "signal",
  rise_time: "axis", fall_time: "axis", settling_time: "axis", pulse_width: "axis",
  propagation_delay: "axis", slew_rate: "signal/axis",
  overshoot: "%", undershoot: "%", duty_cycle: "%", thd: "%",
  forbidden_region_samples: "points",
  frequency: "Hz", cutoff_frequency: "Hz", gain_crossover_frequency: "Hz",
  ac_gain_db: "dB", peaking_db: "dB", gain_margin: "dB", phase_margin: "deg",
};

function requirementTargetUnit(metric, analysis) {
  const kind = METRIC_RESULT_UNITS[metric];
  if (!kind) return "";
  const frequencyDomain = metricDefinition(metric)?.domain === "frequency";
  const axis = analysis?.axis_unit || (frequencyDomain ? "Hz" : "s");
  const signal = analysis?.signal_unit || "";
  if (kind === "signal") return signal;
  if (kind === "axis") return axis;
  if (kind === "signal/axis") return signal ? `${signal}/${axis}` : `per ${axis}`;
  return kind;
}

// Notes left on a requirement whose target was cleared by a metric change,
// shown once on the re-rendered row.
const requirementTargetNotes = new WeakMap();

function setRequirementMetric(requirement, metric) {
  // Only prune against a metric the schema actually describes: a recipe written
  // against a newer build can name one this page has never heard of, and
  // dropping its parameters would quietly rewrite the recipe.
  const definition = metricDefinition(metric);
  if (definition) {
    const accepted = new Set(definition.parameters.map((parameter) => parameter.name));
    for (const key of Object.keys(requirement)) {
      if (REQUIREMENT_CORE_FIELDS.includes(key)) continue;
      if (!accepted.has(key)) delete requirement[key];
    }
  }
  requirement.metric = metric;
}

function buildRequirement(analysis, requirement, index, base, experiment) {
  const container = document.createElement("div");
  container.className = "requirement";
  container.dataset.path = base;

  const row = document.createElement("div");
  row.className = "requirement-row";

  const metric = metricSelect(requirement.metric, `${base}.metric`);
  metric.addEventListener("change", () => {
    const previousUnit = requirementTargetUnit(requirement.metric, analysis);
    const previousTarget = requirement.target;
    setRequirementMetric(requirement, metric.value);
    const nextUnit = requirementTargetUnit(requirement.metric, analysis);
    // A number typed as dB means nothing in Hz: clear it rather than let the
    // old target silently carry over into the new metric's unit.
    if (previousUnit !== nextUnit && previousTarget !== "" && previousTarget !== undefined) {
      requirement.target = "";
      requirementTargetNotes.set(
        requirement,
        `Target cleared: ${previousTarget}${previousUnit ? ` ${previousUnit}` : ""} does not carry over to ${nextUnit || "this metric"}.`,
      );
    }
    populateExperiments();
    schedulePreview();
    if (requirementTargetNotes.has(requirement)) {
      document.querySelector(`[data-path="${base}.target"]`)?.focus();
    }
  });
  const operator = selectInput(requirement.operator, [["<", "<"], ["<=", "\u2264"], [">", ">"], [">=", "\u2265"]], `${base}.operator`);
  setRecipeField(operator, requirement, "operator");
  operator.addEventListener("change", schedulePreview);
  const target = fieldInput(requirement.target, `${base}.target`);
  const targetUnit = requirementTargetUnit(requirement.metric, analysis);
  target.placeholder = "Target";
  target.setAttribute("aria-label", `${readableLabel(`${base}.target`)}${targetUnit ? ` in ${targetUnit}` : ""}`);
  setRecipeField(target, requirement, "target", true);
  target.addEventListener("input", () => {
    requirementTargetNotes.delete(requirement);
    targetNote.hidden = true;
  });
  const targetField = document.createElement("span");
  targetField.className = "target-field";
  targetField.append(target);
  if (targetUnit) {
    const unit = document.createElement("span");
    unit.className = "target-unit";
    unit.textContent = targetUnit;
    unit.setAttribute("aria-hidden", "true");
    targetField.append(unit);
  }
  const targetNote = document.createElement("span");
  targetNote.className = "field-problem target-note";
  targetNote.textContent = requirementTargetNotes.get(requirement) || "";
  targetNote.hidden = !targetNote.textContent;

  row.append(metric, operator, targetField, removeButton(`Remove ${requirement.metric} requirement`, () => {
    analysis.requirements.splice(index, 1);
    populateExperiments();
    schedulePreview();
  }));
  container.append(row, targetNote);

  const parameters = metricParameters(requirement.metric);
  const sweep = acSweepRange(experimentNetlistText(experiment));
  const specific = parameters.filter((parameter) => !parameter.common);
  const shared = parameters.filter((parameter) => parameter.common);
  if (specific.length) {
    const grid = document.createElement("div");
    grid.className = "requirement-parameters";
    for (const parameter of specific) {
      grid.append(buildRequirementParameter(requirement, parameter, base, sweep));
    }
    container.append(grid);
  }
  // The analysis window applies to every metric and is usually left alone, so
  // it folds away rather than burying the fields that are specific to this one.
  if (shared.length) {
    const details = document.createElement("details");
    details.className = "requirement-window";
    details.open = shared.some((parameter) => requirement[parameter.name] !== undefined);
    const summary = document.createElement("summary");
    summary.textContent = "Analysis window";
    const grid = document.createElement("div");
    grid.className = "requirement-parameters";
    for (const parameter of shared) {
      grid.append(buildRequirementParameter(requirement, parameter, base, sweep));
    }
    details.append(summary, grid);
    container.append(details);
  }
  return container;
}

function requirementParameterHint(parameter, sweep) {
  const notes = [parameter.description];
  if (parameter.axis_interpolated) {
    notes.push(
      sweep
        ? `Swept ${formatHertz(sweep.start)} to ${formatHertz(sweep.stop)}.`
        : "Must fall inside the .AC sweep.",
    );
    notes.push("Read by interpolation in log frequency, not snapped to the nearest simulated point.");
  }
  const fallback = formatDefault(parameter.default);
  if (!parameter.required && fallback) notes.push(`Defaults to ${fallback}.`);
  return notes.filter(Boolean).join(" ");
}

function requirementParameterProblem(parameter, value, sweep) {
  if (value === undefined || value === "") {
    return parameter.required ? "Required for this metric." : "";
  }
  const number = Number(value);
  if (parameter.kind === "choice") return "";
  if (!Number.isFinite(number)) return "Must be a number.";
  if (parameter.kind === "integer" && !Number.isInteger(number)) return "Must be a whole number.";
  if (parameter.axis_interpolated && sweep && (number < sweep.start || number > sweep.stop)) {
    return `Outside the .AC sweep (${formatHertz(sweep.start)} to ${formatHertz(sweep.stop)}).`;
  }
  return "";
}

function buildRequirementParameter(requirement, parameter, base, sweep) {
  const wrapper = document.createElement("label");
  wrapper.className = parameter.required
    ? "requirement-parameter required-parameter"
    : "requirement-parameter";
  const path = `${base}.${parameter.name}`;

  const caption = document.createElement("span");
  // "cutoff_drop_db" with unit dB reads as "Cutoff drop (dB)".
  const unitSuffix = parameter.unit ? `_${parameter.unit.toLowerCase()}` : "";
  const baseName = unitSuffix && parameter.name.toLowerCase().endsWith(unitSuffix)
    ? parameter.name.slice(0, -unitSuffix.length)
    : parameter.name;
  caption.textContent = parameter.unit
    ? `${humanizeKey(baseName)} (${parameter.unit})`
    : humanizeKey(baseName);
  caption.title = parameter.name;
  if (parameter.required) {
    const mark = document.createElement("abbr");
    mark.className = "required-mark";
    mark.textContent = "*";
    mark.title = "Required for this metric";
    caption.append(" ", mark);
  }

  const problem = document.createElement("span");
  problem.className = "field-problem";

  let control;
  if (parameter.kind === "choice") {
    control = document.createElement("select");
    control.dataset.path = path;
    control.setAttribute("aria-label", readableLabel(path));
    const fallback = formatDefault(parameter.default);
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = fallback ? `\u2014 default (${fallback}) \u2014` : "\u2014 automatic \u2014";
    control.append(blank);
    for (const choice of parameter.choices) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice;
      control.append(option);
    }
    control.value = requirement[parameter.name] === undefined ? "" : String(requirement[parameter.name]);
    control.addEventListener("change", () => {
      if (control.value === "") delete requirement[parameter.name];
      else requirement[parameter.name] = control.value;
      refresh();
      schedulePreview();
    });
  } else {
    control = fieldInput(requirement[parameter.name], path);
    control.placeholder = formatDefault(parameter.default) || (parameter.required ? "required" : "optional");
    control.addEventListener("input", () => {
      const entered = control.value.trim();
      // An absent key is how an optional parameter says "use the default", so
      // a cleared field deletes rather than writing an empty string.
      if (entered === "") {
        delete requirement[parameter.name];
      } else {
        // Recipes carry plain numbers, but "50k" is how this value is written
        // in the netlist next to it, so accept that spelling and resolve it.
        const scaled = spiceNumber(entered);
        requirement[parameter.name] = Number.isFinite(scaled) ? scaled : numericValue(entered);
      }
      refresh();
      schedulePreview();
    });
  }
  if (parameter.required) control.setAttribute("aria-required", "true");

  const hint = document.createElement("span");
  hint.className = "field-hint";
  hint.textContent = requirementParameterHint(parameter, sweep);

  const note = document.createElement("span");
  note.className = "field-note";

  function refresh() {
    const stored = requirement[parameter.name];
    const message = requirementParameterProblem(parameter, stored, sweep);
    problem.textContent = message;
    problem.hidden = !message;
    wrapper.classList.toggle("has-problem", Boolean(message));
    // Show what a suffixed entry resolved to, since the recipe stores the
    // resolved number rather than the text that was typed.
    const resolved =
      parameter.kind !== "choice" && Number.isFinite(Number(stored))
        && control.value.trim() !== "" && Number(control.value.trim()) !== Number(stored);
    note.textContent = resolved
      ? `Reads as ${parameter.unit === "Hz" ? formatHertz(stored) : stored}.`
      : "";
    note.hidden = !resolved;
  }
  refresh();

  wrapper.append(caption, control, note, hint, problem);
  return wrapper;
}

function emptyEditor(message) {
  const empty = document.createElement("p");
  empty.className = "editor-empty";
  empty.textContent = message;
  return empty;
}

function populateRecipeControls() {
  markClean("save-status", (v) => { studyDirty = v; });
  byId("study-name").textContent = recipe.name || "Untitled study";
  byId("study-description").textContent = recipe.description || "Portable LTspice study recipe";
  populateStudyIdentity();
  byId("sample-count").value = recipe.plan.sample_count;
  byId("seed").value = recipe.plan.seed;
  byId("sampling-method").value = recipe.plan.sampling_method || "independent";
  const execution = recipe.execution || (recipe.execution = {});
  byId("max-concurrency").value = execution.max_concurrency ?? 2;
  byId("reuse-cache").checked = execution.reuse_cache !== false;
  populateVariables();
  populateCorrelations();
  populateCorners();
  populateExperiments();
  populateSchematicControls();
}

// Recipe metadata and the report narrative. These were readable in the header
// but had no inputs, so a study could not be renamed or described in the tool
// that builds it, and five of the seven report_context fields were unreachable.
const IDENTITY_FIELDS = [
  ["identity-name", "recipe", "name"],
  ["identity-description", "recipe", "description"],
  ["identity-title", "report_context", "title"],
  ["identity-circuit-summary", "report_context", "circuit_summary"],
  ["identity-simulation-summary", "report_context", "simulation_summary"],
  ["identity-schematic-caption", "report_context", "schematic_caption"],
  ["identity-mcp-context", "report_context", "mcp_context"],
];

function populateStudyIdentity() {
  const block = byId("study-identity");
  block.hidden = false;
  const context = recipe.report_context || {};
  for (const [id, scope, key] of IDENTITY_FIELDS) {
    byId(id).value = (scope === "recipe" ? recipe[key] : context[key]) ?? "";
  }
}

function bindStudyIdentity() {
  for (const [id, scope, key] of IDENTITY_FIELDS) {
    byId(id).addEventListener("input", (event) => {
      if (!recipe) return;
      const value = event.target.value.trim();
      if (scope === "recipe") {
        // name and description are required top-level strings, so they are
        // written through even when blank and the validator reports them.
        recipe[key] = event.target.value;
        if (key === "name") byId("study-name").textContent = value || "Untitled study";
        if (key === "description") {
          byId("study-description").textContent = value || "Portable LTspice study recipe";
        }
      } else {
        // report_context values must be 1-1,200 non-blank characters when
        // present, so an emptied field drops the key instead of sending "".
        const context = recipe.report_context || (recipe.report_context = {});
        if (value) context[key] = event.target.value;
        else delete context[key];
        if (Object.keys(context).length === 0) delete recipe.report_context;
      }
      schedulePreview();
    });
  }
}

bindStudyIdentity();

// --- Waveform viewer ------------------------------------------------------
// Reads the .raw files a finished run already wrote. Nothing here launches
// LTspice or writes an artifact; the generated HTML report stays the record.
let waveformCaptures = [];
let waveformData = null;
let waveformSelectionPath = null;
const waveformHidden = new Set();
const MAX_DEFAULT_TRACES = 6;

const TRACE_COLORS = ["#e08a4b", "#5fa8c9", "#4fae78", "#d97575", "#b48ead", "#d9a64e"];

async function openWaveforms(experimentId) {
  const panel = byId("waveform-panel");
  panel.hidden = false;
  byId("waveform-title").textContent = `Captured traces · ${experimentId}`;
  waveformError("");
  waveformHidden.clear();
  waveformSelectionPath = null;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(experimentId)}/captures`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Captures could not be listed");
    waveformCaptures = result.captures || [];
  } catch (error) {
    waveformCaptures = [];
    waveformError(error.message);
  }
  const select = byId("waveform-capture");
  if (waveformCaptures.length === 0) {
    select.replaceChildren();
    byId("waveform-plot").replaceChildren();
    byId("waveform-traces").replaceChildren();
    byId("waveform-meta").textContent = "";
    const job = trackedJobs.get(experimentId)
      || (latestHistory?.jobs || []).find((item) => item.experiment_id === experimentId);
    const errored = Number(job?.error_points || 0);
    waveformError(errored
      ? `This run wrote no .raw captures: ${errored} point${errored === 1 ? "" : "s"} did not simulate${job.point_error ? ` (${job.point_error})` : ""}.`
      : "This run wrote no .raw captures. Compressed or cleaned runs keep only their report.");
    return;
  }
  select.replaceChildren(...waveformCaptures.map((capture) => {
    const option = document.createElement("option");
    option.value = capture.path;
    option.textContent = capture.point_index === null
      ? capture.filename
      : `point ${capture.point_index} · ${capture.filename}`;
    return option;
  }));
  select.value = waveformCaptures[0].path;
  panel.scrollIntoView({behavior: "smooth", block: "nearest"});
  await loadWaveform();
}

function waveformError(message) {
  const box = byId("waveform-errors");
  box.textContent = message;
  box.hidden = !message;
}

async function loadWaveform() {
  const path = byId("waveform-capture").value;
  if (!path) return;
  const maxPoints = byId("waveform-resolution").value;
  byId("waveform-csv").href = `/api/waveform.csv?path=${encodeURIComponent(path)}`;
  try {
    const response = await fetch(
      `/api/waveform?path=${encodeURIComponent(path)}&max_points=${maxPoints}`,
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Waveform could not be read");
    waveformData = result;
    waveformError("");
    // A new capture gets a fresh, readable selection; changing only the
    // resolution keeps whatever the reader has chosen.
    if (waveformSelectionPath !== path) {
      waveformSelectionPath = path;
      waveformHidden.clear();
      // An operating point is a short list of values, not a plot to declutter.
      if (!result.operating_point) {
        for (const name of defaultHiddenTraces(result)) waveformHidden.add(name);
      }
      byId("waveform-scale").value = result.complex ? "db" : "linear";
    }
  } catch (error) {
    waveformData = null;
    waveformError(error.message);
    byId("waveform-plot").replaceChildren();
    return;
  }
  renderTraceToggles();
  renderWaveformPlot();
  refreshWaveformMeta();
}

function refreshWaveformMeta() {
  const data = waveformData;
  if (!data) { byId("waveform-meta").textContent = ""; return; }
  const steps = data.step_count > 1 ? ` · ${data.step_count} stepped blocks` : "";
  if (data.operating_point) {
    const count = Object.keys(data.series).length;
    byId("waveform-meta").textContent = `Operating point · ${count} values`
      + (data.total_points > 1 ? ` · ${data.total_points} steps` : "");
    return;
  }
  byId("waveform-meta").textContent =
    `${data.returned_points.toLocaleString()} of ${data.total_points.toLocaleString()} points`
    + ` · axis ${data.axis_variable} (${data.axis_unit})`
    + (data.complex
      ? (byId("waveform-scale").value === "db"
        ? " · AC capture, magnitude in dB"
        : " · AC capture, plotted as magnitude")
      : "")
    + steps;
}

function renderTraceToggles() {
  const data = waveformData;
  const names = Object.keys(data.series);
  // Grouped by unit, because that is also how they are plotted: a reader who
  // sees two axes needs to know which trace belongs to which.
  const groups = new Map();
  for (const name of names) {
    const unit = traceUnit(data, name);
    if (!groups.has(unit)) groups.set(unit, []);
    groups.get(unit).push(name);
  }
  const blocks = [...groups.entries()].map(([unit, members]) => {
    const block = document.createElement("div");
    block.className = "trace-group";
    if (groups.size > 1) {
      const caption = document.createElement("span");
      caption.className = "trace-group-unit";
      caption.textContent = unit || "unitless";
      block.append(caption);
    }
    for (const name of members) {
      const label = document.createElement("label");
      label.className = "trace-toggle";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !waveformHidden.has(name);
      box.setAttribute("aria-label", `Plot ${name}`);
      box.addEventListener("change", () => {
        if (box.checked) waveformHidden.delete(name);
        else waveformHidden.add(name);
        renderWaveformPlot();
      });
      const swatch = document.createElement("span");
      swatch.className = "trace-swatch";
      swatch.style.background = TRACE_COLORS[names.indexOf(name) % TRACE_COLORS.length];
      const text = document.createElement("span");
      text.textContent = name;
      label.append(box, swatch, text);
      block.append(label);
    }
    return block;
  });
  byId("waveform-traces").replaceChildren(...blocks);
}

function traceUnit(data, name) {
  return (data.units && data.units[name]) || "";
}

// A capture holds every node and every device current the deck produced.
// Plotting all of them at once is unreadable, so the viewer opens on one
// coherent family -- voltages where there are any -- and the rest are one
// click away.
function defaultHiddenTraces(data) {
  const names = Object.keys(data.series);
  const counts = new Map();
  for (const name of names) {
    const unit = traceUnit(data, name);
    counts.set(unit, (counts.get(unit) || 0) + 1);
  }
  const ranked = [...counts.entries()].sort((left, right) => right[1] - left[1]);
  const primary = counts.has("V") ? "V" : (ranked[0]?.[0] ?? "");
  // Outputs first: they are what a reader opens a capture to see, and a deck
  // usually declares its supply and input nodes ahead of them.
  const isOutput = (name) => /out/i.test(name);
  const preferred = names
    .filter((name) => traceUnit(data, name) === primary)
    .sort((left, right) => Number(isOutput(right)) - Number(isOutput(left)));
  const visible = new Set((preferred.length ? preferred : names).slice(0, MAX_DEFAULT_TRACES));
  return new Set(names.filter((name) => !visible.has(name)));
}

// Engineering notation with the SI prefix, the way LTspice's own .op
// listing reads: 1.25 V, 2 mA, 15 pA.
function formatSi(value, unit) {
  if (!Number.isFinite(value)) return String(value);
  if (value === 0) return `0${unit ? ` ${unit}` : ""}`;
  const prefixes = [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"], [1e-15, "f"]];
  const magnitude = Math.abs(value);
  // Below femto is solver noise around zero; an exponent says so honestly.
  if (magnitude < 1e-15) return `${value.toExponential(2)}${unit ? ` ${unit}` : ""}`;
  const [factor, prefix] = prefixes.find(([candidate]) => magnitude >= candidate) || prefixes[prefixes.length - 1];
  const text = String(Number((value / factor).toPrecision(5)));
  return unit || prefix ? `${text} ${prefix}${unit}` : text;
}

// An operating point has no axis to plot against; it is read as a table,
// one column per step when the .op was stepped.
function operatingPointTable(data, shown) {
  const MAX_COLUMNS = 12;
  const columns = Math.min(data.axis.length, MAX_COLUMNS);
  const table = document.createElement("table");
  table.className = "editor-table waveform-op-table";
  const head = document.createElement("tr");
  const headings = ["Vector", ...(columns === 1
    ? ["Value"]
    : Array.from({length: columns}, (_, index) => `Step ${index + 1}`))];
  for (const text of headings) {
    const cell = document.createElement("th");
    cell.textContent = text;
    head.append(cell);
  }
  const thead = document.createElement("thead");
  thead.append(head);
  const body = document.createElement("tbody");
  for (const [name, values] of shown) {
    const row = document.createElement("tr");
    const label = document.createElement("td");
    label.textContent = name;
    row.append(label);
    for (const value of values.slice(0, columns)) {
      const cell = document.createElement("td");
      cell.textContent = formatSi(value, traceUnit(data, name));
      row.append(cell);
    }
    body.append(row);
  }
  table.append(thead, body);
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  wrap.append(table);
  if (data.axis.length > MAX_COLUMNS) {
    const note = byId("waveform-note");
    note.textContent = `Showing the first ${MAX_COLUMNS} of ${data.axis.length} steps; Export CSV has them all.`;
    note.hidden = false;
  }
  return wrap;
}

function svgElement(name, attributes) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function decibels(value) {
  // 0 is negative infinity dB; leaving it non-finite breaks the trace there,
  // which is the honest rendering of a null.
  return value === 0 ? -Infinity : 20 * Math.log10(Math.abs(value));
}

// Volts and amperes must never share a linear axis -- a milliamp trace drawn
// against a 3.3 V range is a flat line on the baseline, which is what makes a
// capture look empty. Each unit gets its own vertical scale instead. In dB
// everything is already dimensionless, so one axis serves.
function traceGroups(shown, data, mode) {
  if (mode === "db") return [{unit: "dB", traces: shown}];
  const byUnit = new Map();
  for (const entry of shown) {
    const unit = traceUnit(data, entry[0]);
    if (!byUnit.has(unit)) byUnit.set(unit, []);
    byUnit.get(unit).push(entry);
  }
  return [...byUnit.entries()].map(([unit, traces]) => ({unit, traces}));
}

function groupRange(group, mode) {
  let low = Infinity;
  let high = -Infinity;
  for (const [, values] of group.traces) {
    for (const sample of values) {
      const value = mode === "db" ? decibels(sample) : sample;
      if (!Number.isFinite(value)) continue;
      if (value < low) low = value;
      if (value > high) high = value;
    }
  }
  if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
  if (low === high) { low -= 1; high += 1; }
  const padding = (high - low) * 0.08;
  return {low: low - padding, high: high + padding};
}

function renderWaveformPlot() {
  const data = waveformData;
  const host = byId("waveform-plot");
  const note = byId("waveform-note");
  note.hidden = true;
  if (!data) { host.replaceChildren(); return; }
  const mode = byId("waveform-scale").value;
  const shown = Object.entries(data.series).filter(([name]) => !waveformHidden.has(name));
  if (!shown.length) {
    host.replaceChildren(emptyEditor("Select at least one trace."));
    return;
  }
  if (data.axis.length === 0) {
    host.replaceChildren(emptyEditor("This capture holds no samples."));
    return;
  }
  if (data.operating_point) {
    host.replaceChildren(operatingPointTable(data, shown));
    return;
  }

  // Two vertical scales are drawn, left and right. A third unit would need a
  // third axis nobody can read, so it is named instead of silently flattened.
  const groups = traceGroups(shown, data, mode).map((group) => ({...group, range: groupRange(group, mode)}));
  const plotted = groups.filter((group) => group.range).slice(0, 2);
  const dropped = groups.filter((group) => !plotted.includes(group));
  if (!plotted.length) {
    host.replaceChildren(emptyEditor(
      mode === "db"
        ? "Every selected trace is zero, which is negative infinity dB. Switch the vertical scale to linear."
        : "This capture holds no finite samples.",
    ));
    return;
  }
  if (dropped.length) {
    const flat = dropped.filter((group) => !group.range).map((group) => group.unit || "unitless");
    const extra = dropped.filter((group) => group.range).map((group) => group.unit || "unitless");
    const parts = [];
    if (extra.length) parts.push(`${extra.join(" and ")} needs its own axis — deselect one of the plotted families to see it`);
    if (flat.length) parts.push(`${flat.join(" and ")} holds no finite samples`);
    note.textContent = parts.join(". ") + ".";
    note.hidden = false;
  }

  const width = 860;
  const height = 360;
  const pad = {left: 78, right: plotted.length > 1 ? 78 : 20, top: 18, bottom: 46};
  const axis = data.axis;
  // AC captures span decades, so the frequency axis is drawn logarithmically;
  // a transient axis stays linear.
  const logAxis = data.axis_unit === "Hz" && axis[0] > 0;
  const project = (value) => (logAxis ? Math.log10(value) : value);
  const xMin = project(axis[0]);
  const xMax = project(axis[axis.length - 1]);
  const xAt = (value) => pad.left + ((project(value) - xMin) / (xMax - xMin || 1)) * (width - pad.left - pad.right);
  const yFor = (group) => (value) =>
    height - pad.bottom - ((value - group.range.low) / (group.range.high - group.range.low)) * (height - pad.top - pad.bottom);

  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
    "aria-label": `${data.filename} waveform`,
  });
  svg.classList.add("waveform-svg");

  for (let index = 0; index <= 4; index += 1) {
    const gx = pad.left + index * (width - pad.left - pad.right) / 4;
    const gy = pad.top + index * (height - pad.top - pad.bottom) / 4;
    svg.append(
      svgElement("line", {x1: gx, y1: pad.top, x2: gx, y2: height - pad.bottom, class: "plot-grid"}),
      svgElement("line", {x1: pad.left, y1: gy, x2: width - pad.right, y2: gy, class: "plot-grid"}),
    );
    const xValue = logAxis
      ? 10 ** (xMin + index * (xMax - xMin) / 4)
      : xMin + index * (xMax - xMin) / 4;
    const xTick = svgElement("text", {x: gx, y: height - pad.bottom + 20, class: "plot-tick", "text-anchor": "middle"});
    xTick.textContent = axisLabel(xValue, data.axis_unit);
    svg.append(xTick);

    plotted.forEach((group, side) => {
      const value = group.range.high - index * (group.range.high - group.range.low) / 4;
      const tick = svgElement("text", {
        x: side === 0 ? pad.left - 9 : width - pad.right + 9,
        y: gy + 4,
        class: "plot-tick",
        "text-anchor": side === 0 ? "end" : "start",
      });
      tick.textContent = axisLabel(value, group.unit === "dB" ? "dB" : group.unit);
      svg.append(tick);
    });
  }

  const names = Object.keys(data.series);
  const singlePoint = axis.length === 1;
  for (const group of plotted) {
    const yAt = yFor(group);
    for (const [name, values] of group.traces) {
      const color = TRACE_COLORS[names.indexOf(name) % TRACE_COLORS.length];
      let path = "";
      let pen = false;
      for (let index = 0; index < values.length; index += 1) {
        const value = mode === "db" ? decibels(values[index]) : values[index];
        if (!Number.isFinite(value)) { pen = false; continue; }
        // An operating point, or a run that stopped after one step, has a
        // single sample. A line cannot show that, so it is drawn as a marker.
        if (singlePoint) {
          svg.append(svgElement("circle", {cx: xAt(axis[index]), cy: yAt(value), r: 3.5, fill: color}));
          continue;
        }
        path += `${pen ? "L" : "M"}${xAt(axis[index]).toFixed(2)} ${yAt(value).toFixed(2)}`;
        pen = true;
      }
      if (path) {
        svg.append(svgElement("path", {d: path, fill: "none", stroke: color, "stroke-width": "1.6", "stroke-linejoin": "round"}));
      }
    }
  }

  host.replaceChildren(svg);
}

function axisLabel(value, unit) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (unit === "dB") return `${Number(number.toPrecision(3))} dB`;
  const magnitude = Math.abs(number);
  const scales = unit === "Hz"
    ? [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""]]
    : unit === "s"
      ? [[1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"]]
      : [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"]];
  const [factor, prefix] = scales.find(([candidate]) => magnitude >= candidate) || scales[scales.length - 1];
  return `${Number((number / factor).toPrecision(3))}${prefix ? " " + prefix : ""}${unit}`;
}

// --- Adaptive boundary ----------------------------------------------------
// Bisects one variable between a passing and a failing sampled point. Each
// advance takes in the finished batch and launches the next, so the loop is
// driven from here rather than run to completion in one call.
let boundarySource = null;
let boundaryStudy = null;

function openBoundary(experimentId) {
  boundarySource = experimentId;
  boundaryStudy = null;
  byId("boundary-title").textContent = `Where the requirement turns over · ${experimentId}`;
  byId("boundary-state").hidden = true;
  boundaryError("");
  const panel = byId("boundary-panel");
  panel.hidden = false;
  panel.scrollIntoView({behavior: "smooth", block: "nearest"});
  loadBoundaryCandidates(experimentId);
}

// Check ids are content hashes, so the panel offers the brackets the run can
// actually seed -- or says why there are none -- instead of asking for one.
let boundaryCandidates = [];

async function loadBoundaryCandidates(experimentId) {
  const select = byId("boundary-candidate");
  const note = byId("boundary-candidates-note");
  const button = byId("boundary-define");
  boundaryCandidates = [];
  select.replaceChildren();
  select.disabled = true;
  button.disabled = true;
  note.textContent = "Looking for brackets…";
  note.hidden = false;
  try {
    const response = await fetch(`/api/boundary/candidates/${encodeURIComponent(experimentId)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "This run's points could not be read");
    if (boundarySource !== experimentId) return;
    boundaryCandidates = result.candidates || [];
    if (!boundaryCandidates.length) {
      note.textContent = `None of the ${result.evaluated_points} evaluated points pair up: a bracket needs two points that differ in one variable only and fall on opposite sides of a check. Sweep that variable, or add it as a corner axis, and bracket from that run.`;
      return;
    }
    select.replaceChildren(...boundaryCandidates.map((candidate, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = `${candidate.check} · ${candidate.variable} · point ${candidate.passing_point} passes, ${candidate.failing_point} fails`;
      return option;
    }));
    select.disabled = false;
    button.disabled = false;
    note.textContent = result.total_candidates > boundaryCandidates.length
      ? `Showing ${boundaryCandidates.length} of ${result.total_candidates} brackets.`
      : "";
    note.hidden = !note.textContent;
  } catch (error) {
    note.hidden = true;
    boundaryError(error.message);
  }
}

function boundaryError(message) {
  const box = byId("boundary-errors");
  box.textContent = message;
  box.hidden = !message;
}

async function defineBoundary() {
  const candidate = boundaryCandidates[Number(byId("boundary-candidate").value)];
  if (!boundarySource || !candidate) return;
  const button = byId("boundary-define");
  button.disabled = true;
  boundaryError("");
  try {
    const response = await fetch("/api/boundary/define", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        source_experiment_id: boundarySource,
        first_point_index: candidate.passing_point,
        second_point_index: candidate.failing_point,
        check_id: candidate.check_id,
        variable: candidate.variable,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Boundary study failed");
    renderBoundary(result);
  } catch (error) {
    boundaryError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function advanceBoundary() {
  if (!boundaryStudy) return;
  const button = byId("boundary-advance");
  button.disabled = true;
  byId("boundary-status").textContent = "Advancing…";
  try {
    const response = await fetch(
      `/api/boundary/${encodeURIComponent(boundaryStudy.adaptive_id)}/advance`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-LTspice-System-Builder": "1",
        },
        body: "{}",
      },
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Boundary advance failed");
    renderBoundary(result);
    if (result.active_experiment_id) {
      trackJob(result.active_experiment_id, {
        name: "boundary batch",
        experiment_id: result.active_experiment_id,
        status: "running",
      });
      renderTrackedJobs();
      scheduleJobPoll(250);
    }
  } catch (error) {
    byId("boundary-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderBoundary(study) {
  boundaryStudy = study;
  boundaryError("");
  const tiles = [
    ["Status", study.status],
    ["Samples", `${study.sample_count} / ${study.max_samples}`],
    ["Batches", study.batch_count],
    ["Turns over at", `≈ ${formatSi(Number(study.boundary_estimate), boundaryUnit(study.unit))}`],
    [study.low_passed ? "Passes up to" : "Fails up to", formatSi(Number(study.low_input), boundaryUnit(study.unit))],
    [study.low_passed ? "Fails from" : "Passes from", formatSi(Number(study.high_input), boundaryUnit(study.unit))],
    ["Bracket width", formatSi(Number(study.current_width), boundaryUnit(study.unit))],
    ["Tolerance", Number(study.input_tolerance).toPrecision(3)],
    ["Variable", study.variable],
  ].map(([label, value]) => {
    const tile = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const amount = document.createElement("strong");
    amount.textContent = String(value);
    tile.append(caption, amount);
    return tile;
  });
  byId("boundary-metrics").replaceChildren(...tiles);
  byId("boundary-state").hidden = false;
  const finished = Boolean(study.stop_reason);
  byId("boundary-advance").disabled = finished;
  byId("boundary-status").textContent = study.error
    || (finished ? `Converged · ${study.stop_reason}` : `Study ${study.adaptive_id}`);
}

// Recipes spell units out ("ohm", "F"); the panel reads them as symbols.
function boundaryUnit(unit) {
  const symbols = {ohm: "Ω", ohms: "Ω", farad: "F", henry: "H", volt: "V", amp: "A", ampere: "A", hertz: "Hz", second: "s"};
  const text = String(unit || "");
  return symbols[text.toLowerCase()] ?? text;
}

function boundaryButton(experimentId) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "compact-button";
  button.textContent = "Boundary";
  button.title = `Bracket where a requirement turns over in ${experimentId}`;
  button.addEventListener("click", () => {
    showView("history");
    openBoundary(experimentId);
  });
  return button;
}

byId("boundary-define").addEventListener("click", defineBoundary);
byId("boundary-advance").addEventListener("click", advanceBoundary);
byId("boundary-close").addEventListener("click", () => {
  byId("boundary-panel").hidden = true;
});

// --- Local sensitivity ----------------------------------------------------
// Answers "which component actually moves this margin?" for a design point
// that already has electrical evidence, by perturbing each variable above and
// below it one at a time.
let sensitivitySource = null;
let sensitivityAnalysis = null;

function openSensitivity(experimentId) {
  sensitivitySource = experimentId;
  sensitivityAnalysis = null;
  byId("sensitivity-title").textContent = `Which component moves the margin · ${experimentId}`;
  byId("sensitivity-result").hidden = true;
  sensitivityError("");
  const panel = byId("sensitivity-panel");
  panel.hidden = false;
  panel.scrollIntoView({behavior: "smooth", block: "nearest"});
  // A finished study can be read back without re-running it.
  loadSensitivityAnalysis(experimentId, {quiet: true});
}

function sensitivityError(message) {
  const box = byId("sensitivity-errors");
  box.textContent = message;
  box.hidden = !message;
}

async function runSensitivity() {
  if (!sensitivitySource) return;
  const button = byId("sensitivity-run");
  button.disabled = true;
  button.textContent = "Starting…";
  sensitivityError("");
  try {
    const response = await fetch("/api/sensitivity/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        source_experiment_id: sensitivitySource,
        source_point_index: Number(byId("sensitivity-point").value),
        relative_step: Number(byId("sensitivity-step").value),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Sensitivity study failed");
    trackJob(result.experiment_id, {name: "sensitivity", ...result});
    renderTrackedJobs();
    scheduleJobPoll(250);
    sensitivityError("");
    byId("sensitivity-meta").textContent =
      `Study ${result.experiment_id} is running. Its tornado appears here once every point finishes.`;
    byId("sensitivity-result").hidden = false;
    sensitivitySource = result.experiment_id;
  } catch (error) {
    sensitivityError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Run sensitivity study";
  }
}

async function loadSensitivityAnalysis(experimentId, {quiet = false} = {}) {
  try {
    const response = await fetch(`/api/sensitivity/${encodeURIComponent(experimentId)}`);
    const result = await response.json();
    if (!response.ok) {
      if (!quiet) sensitivityError(result.error?.message || "No tornado is available yet");
      return;
    }
    sensitivityAnalysis = result;
    byId("sensitivity-csv").href = result.csv_url;
    const requirements = result.analysis.requirements || [];
    byId("sensitivity-requirement").replaceChildren(...requirements.map((requirement, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = `${requirement.analysis} · ${requirement.metric} ${requirement.operator} ${requirement.target}`;
      return option;
    }));
    byId("sensitivity-result").hidden = false;
    renderTornado();
  } catch (error) {
    if (!quiet) sensitivityError(error.message);
  }
}

// The tornado for a study started here is read once its job settles; nothing
// else would load it, and the panel would keep saying "running".
function settleSensitivityJob() {
  if (!sensitivitySource || sensitivityAnalysis) return;
  const job = trackedJobs.get(sensitivitySource);
  if (!job || job.name !== "sensitivity" || isActiveJob(job)) return;
  if (job.status === "completed") {
    byId("sensitivity-meta").textContent = "";
    loadSensitivityAnalysis(sensitivitySource);
  } else {
    byId("sensitivity-meta").textContent = "";
    sensitivityError(`Sensitivity study ${sensitivitySource} ${job.status}${job.error ? `: ${job.error}` : "."}`);
  }
}

function renderTornado() {
  const host = byId("sensitivity-plot");
  const analysis = sensitivityAnalysis?.analysis;
  const index = Number(byId("sensitivity-requirement").value || 0);
  const requirement = analysis?.requirements?.[index];
  if (!requirement) { host.replaceChildren(emptyEditor("No completed effects yet.")); return; }

  // One bar per variable, widest total swing first: this is the ordering that
  // makes a tornado readable.
  const bars = (requirement.effects || [])
    .filter((effect) => effect.status === "complete")
    .map((effect) => ({
      name: effect.name,
      low: Number(effect.low_effect) || 0,
      high: Number(effect.high_effect) || 0,
    }))
    .sort((a, b) => (Math.abs(b.low) + Math.abs(b.high)) - (Math.abs(a.low) + Math.abs(a.high)));
  if (!bars.length) {
    host.replaceChildren(emptyEditor("This requirement has no complete effects — some perturbed points did not finish."));
    return;
  }

  const rowHeight = 26;
  const width = 860;
  const pad = {left: 132, right: 30, top: 26, bottom: 38};
  const height = pad.top + pad.bottom + bars.length * rowHeight;
  const extent = Math.max(...bars.flatMap((bar) => [Math.abs(bar.low), Math.abs(bar.high)]), 1e-12);
  const xAt = (value) => pad.left + ((value + extent) / (2 * extent)) * (width - pad.left - pad.right);

  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `Tornado of margin effects for ${requirement.metric}`,
  });
  svg.classList.add("waveform-svg");

  for (let step = 0; step <= 4; step += 1) {
    const value = -extent + step * (2 * extent) / 4;
    const x = xAt(value);
    svg.append(svgElement("line", {x1: x, y1: pad.top, x2: x, y2: height - pad.bottom, class: "plot-grid"}));
    const tick = svgElement("text", {x, y: height - pad.bottom + 20, class: "plot-tick", "text-anchor": "middle"});
    tick.textContent = Number(value.toPrecision(3)).toString();
    svg.append(tick);
  }

  bars.forEach((bar, row) => {
    const y = pad.top + row * rowHeight;
    const label = svgElement("text", {x: pad.left - 10, y: y + rowHeight / 2 + 4, class: "plot-tick", "text-anchor": "end"});
    label.textContent = bar.name;
    svg.append(label);
    for (const [value, color] of [[bar.low, "#5fa8c9"], [bar.high, "#e08a4b"]]) {
      if (value === 0) continue;
      const from = Math.min(xAt(0), xAt(value));
      svg.append(svgElement("rect", {
        x: from,
        y: y + 5,
        width: Math.max(Math.abs(xAt(value) - xAt(0)), 1),
        height: rowHeight - 12,
        fill: color,
        opacity: "0.85",
        rx: "2",
      }));
    }
  });
  const zero = xAt(0);
  svg.append(svgElement("line", {x1: zero, y1: pad.top, x2: zero, y2: height - pad.bottom, stroke: "currentColor", "stroke-width": "1", opacity: "0.5"}));

  host.replaceChildren(svg);
  byId("sensitivity-meta").textContent =
    `${bars.length} variables · baseline margin ${Number(requirement.baseline_margin).toPrecision(4)}`
    + ` · ±${(analysis.relative_step * 100).toFixed(2)}% step`
    + ` · blue is the low perturbation, copper the high`;
}

function sensitivityButton(experimentId) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "compact-button";
  button.textContent = "Sensitivity";
  button.title = `Find which variable moves ${experimentId}'s margins`;
  button.addEventListener("click", () => {
    showView("history");
    openSensitivity(experimentId);
  });
  return button;
}

byId("sensitivity-run").addEventListener("click", runSensitivity);
byId("sensitivity-requirement").addEventListener("change", renderTornado);
byId("sensitivity-close").addEventListener("click", () => {
  byId("sensitivity-panel").hidden = true;
});

// --- History filtering and run comparison ---------------------------------
function matchesHistoryFilter(job) {
  const search = byId("history-search").value.trim().toLowerCase();
  const status = byId("history-status").value;
  const outcome = byId("history-outcome").value;
  if (status && job.status !== status) return false;
  if (outcome === "true" && job.all_passed !== true) return false;
  if (outcome === "false" && job.all_passed !== false) return false;
  if (!search) return true;
  const haystack = [
    job.experiment_id,
    job.status,
    job.execution_mode,
    job.statistical ? "statistical" : "experiment",
  ].join(" ").toLowerCase();
  return haystack.includes(search);
}

function refilterHistory() {
  if (latestHistory) renderHistory(latestHistory);
}

function comparableJobs() {
  return (latestHistory?.jobs || []).filter((job) => job.status === "completed" && !jobAllErrored(job));
}

function openComparePanel() {
  const panel = byId("compare-panel");
  const jobs = comparableJobs();
  compareError("");
  byId("compare-result").hidden = true;
  if (jobs.length < 2) {
    panel.hidden = false;
    compareError("Two completed runs are needed before anything can be compared.");
    byId("compare-baseline").replaceChildren();
    byId("compare-candidate").replaceChildren();
    return;
  }
  for (const [id, defaultIndex] of [["compare-baseline", 1], ["compare-candidate", 0]]) {
    const select = byId(id);
    select.replaceChildren(...jobs.map((job) => {
      const option = document.createElement("option");
      option.value = job.experiment_id;
      // The id alone is unreadable in a long list; the full id stays in the
      // tooltip for telling apart two runs of the same study.
      option.textContent = `${jobDisplayName(job)} · ${relativeTime(job.recorded_at)} · ${job.passed_points}/${job.point_count} pass`;
      option.title = job.experiment_id;
      return option;
    }));
    select.value = jobs[Math.min(defaultIndex, jobs.length - 1)].experiment_id;
  }
  panel.hidden = false;
  panel.scrollIntoView({behavior: "smooth", block: "nearest"});
}

function compareError(message) {
  const box = byId("compare-errors");
  box.textContent = message;
  box.hidden = !message;
}

async function runComparison() {
  const baseline = byId("compare-baseline").value;
  const candidate = byId("compare-candidate").value;
  if (!baseline || !candidate) return;
  if (baseline === candidate) {
    compareError("Choose two different runs.");
    return;
  }
  const button = byId("compare-run");
  button.disabled = true;
  button.textContent = "Comparing…";
  compareError("");
  try {
    const response = await fetch("/api/compare", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({
        baseline_experiment_id: baseline,
        candidate_experiment_id: candidate,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Comparison failed");
    renderComparison(result);
  } catch (error) {
    byId("compare-result").hidden = true;
    compareError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Compare";
  }
}

function renderComparison(result) {
  const tiles = [
    ["Regressions", result.requirement_regressions],
    ["Improvements", result.requirement_improvements],
    ["Unchanged", result.unchanged_requirements],
    ["Matched points", result.matched_points],
    ["Added / removed points", `${result.added_points} / ${result.removed_points}`],
    ["Added / removed requirements", `${result.added_requirements} / ${result.removed_requirements}`],
  ].map(([label, value]) => {
    const tile = document.createElement("div");
    if (label === "Regressions" && Number(value) > 0) tile.className = "accent-metric regression";
    const caption = document.createElement("span");
    caption.textContent = label;
    const amount = document.createElement("strong");
    amount.textContent = typeof value === "number" ? value.toLocaleString() : value;
    tile.append(caption, amount);
    return tile;
  });
  byId("compare-metrics").replaceChildren(...tiles);
  byId("compare-links").replaceChildren(reportLink(result.report_url, "Open comparison ↗"));
  byId("compare-result").hidden = false;
}

for (const id of ["history-search", "history-status", "history-outcome"]) {
  byId(id).addEventListener("input", refilterHistory);
}
byId("history-limit").addEventListener("change", () => loadHistory(false));
byId("open-compare").addEventListener("click", openComparePanel);
byId("compare-run").addEventListener("click", runComparison);
byId("compare-close").addEventListener("click", () => {
  byId("compare-panel").hidden = true;
});

function waveformButton(experimentId) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "compact-button";
  button.textContent = "Waveforms";
  button.title = `Plot the .raw captures ${experimentId} wrote`;
  button.addEventListener("click", () => {
    showView("history");
    openWaveforms(experimentId);
  });
  return button;
}

byId("waveform-capture").addEventListener("change", loadWaveform);
byId("waveform-resolution").addEventListener("change", loadWaveform);
byId("waveform-scale").addEventListener("change", () => {
  renderWaveformPlot();
  refreshWaveformMeta();
});
byId("waveform-select-all").addEventListener("click", () => {
  waveformHidden.clear();
  renderTraceToggles();
  renderWaveformPlot();
});
byId("waveform-select-none").addEventListener("click", () => {
  if (!waveformData) return;
  for (const name of Object.keys(waveformData.series)) waveformHidden.add(name);
  renderTraceToggles();
  renderWaveformPlot();
});
byId("waveform-close").addEventListener("click", () => {
  byId("waveform-panel").hidden = true;
});

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

// True when `path` is `ancestor` itself or lies beneath it on a segment
// boundary, so experiments[1] never claims experiments[10]'s errors and
// plan.variables does not match plan.variables_extra.
function isPathWithin(path, ancestor) {
  if (path === ancestor) return true;
  if (!path.startsWith(ancestor)) return false;
  const next = path.charAt(ancestor.length);
  return next === "." || next === "[";
}

// Flags only the most specific element an error points at: the field whose
// path matches exactly, or failing that the closest enclosing element (a
// requirement card, a variable row). Controls get aria-invalid; containers
// get an outline, since aria-invalid means nothing on a <section> or <tr>.
function markErrorPath(errorPath) {
  const elements = [...document.querySelectorAll("[data-path]")];
  let targets = elements.filter((element) => element.dataset.path === errorPath);
  if (targets.length === 0) {
    const ancestors = elements.filter((element) => isPathWithin(errorPath, element.dataset.path));
    const longest = Math.max(...ancestors.map((element) => element.dataset.path.length), -1);
    targets = ancestors.filter((element) => element.dataset.path.length === longest);
  }
  for (const element of targets) {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(element.tagName)) {
      element.setAttribute("aria-invalid", "true");
    } else {
      element.classList.add("scope-invalid");
    }
  }
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
    element.classList.remove("scope-invalid");
  });
  for (const [id, prefixes] of scopes) {
    const matched = (errors || []).filter((error) => prefixes.some((prefix) => isPathWithin(error.path, prefix)));
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
      markErrorPath(error.path);
    }
    container.replaceChildren(list);
    container.hidden = false;
  }
  // Errors outside every scoped list (plan.sample_count, execution.*) are
  // still listed in the preview panel; flag their field in place as well.
  for (const error of errors || []) {
    if (!scopes.some(([, prefixes]) => prefixes.some((prefix) => isPathWithin(error.path, prefix)))) {
      markErrorPath(error.path);
    }
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
  // A re-preview that did not come from a recipe edit (netlist rescan or
  // save) keeps the frozen plan only while it still resolves to that plan.
  if (frozenLaunch && (
    !result.valid
    || result.plan?.plan_id !== frozenLaunch.plan.plan_id
    || result.recipe?.sha256 !== frozenLaunch.recipe_sha256
  )) {
    invalidateFrozenPlan();
  }
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
  byId("freeze-button").disabled = Boolean(frozenLaunch);
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

// Errors from Cancel/Resume/Build report, keyed by experiment id, so they
// render next to the button that failed -- in whichever view it lives -- and
// survive the re-render the next status poll does.
const jobActionErrors = new Map();

function jobActionError(experimentId) {
  const message = jobActionErrors.get(experimentId);
  if (!message) return null;
  const error = document.createElement("span");
  error.className = "job-error";
  error.setAttribute("role", "alert");
  error.textContent = message;
  return error;
}

function jobActionButton(label, action, experimentId, className = "compact-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    jobActionErrors.delete(experimentId);
    button.parentElement?.querySelectorAll(".job-error[role=alert]").forEach((node) => node.remove());
    try {
      await action();
    } catch (error) {
      const message = `${label} failed: ${error.message}`;
      jobActionErrors.set(experimentId, message);
      button.after(jobActionError(experimentId));
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
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

// Tracked jobs belong to the study that launched them. The Study setup panel
// shows only the open study's jobs, so opening another project never shows
// the previous one's cards; polling still covers every tracked job.
function studyProjectKey() {
  if (currentStudyProjectSlug) return `project:${currentStudyProjectSlug}`;
  return recipe ? `recipe:${recipe.name || ""}` : null;
}

function studyTitle() {
  return recipe ? (recipe.report_context?.title || recipe.name || "") : "";
}

function trackJob(experimentId, data, project = null) {
  const previous = trackedJobs.get(experimentId);
  trackedJobs.set(experimentId, {...previous, ...data, project: previous?.project ?? project});
}

// Jobs recovered from history (a reload mid-run) carry no project; claim the
// ones whose report title is the open study's.
function adoptRecoveredJobs() {
  const key = studyProjectKey();
  const title = studyTitle();
  if (!key || !title) return;
  for (const job of trackedJobs.values()) {
    if (job.project == null && job.study_title === title) job.project = key;
  }
}

function forgetFinishedJobs() {
  for (const [id, job] of trackedJobs) {
    if (!isActiveJob(job)) trackedJobs.delete(id);
  }
}

function isActiveJob(job) {
  return ["defined", "queued", "running", "cancelling"].includes(job.status) || Boolean(job.finalizing);
}

function visibleTrackedJobs() {
  const key = studyProjectKey();
  return key ? [...trackedJobs.values()].filter((job) => job.project === key) : [];
}

// Errored and cancelled points count as failed in the engine; split them out
// so a run where LTspice never produced output does not read as
// "0 pass · 8 fail", and points a cancel interrupted do not read as failures.
function jobPointSummary(job) {
  const finished = Number(job.finished_points || 0);
  const total = Number(job.point_count || 0);
  const errored = Number(job.error_points || 0);
  const cancelled = Number(job.cancelled_points || 0);
  const failed = Math.max(0, Number(job.failed_points || 0) - errored - cancelled);
  const parts = [`${finished}/${total} points`, `${job.passed_points || 0} pass`, `${failed} fail`];
  if (errored) parts.push(`${errored} error`);
  if (cancelled) parts.push(`${cancelled} interrupted`);
  return parts.join(" · ");
}

function jobAllErrored(job) {
  const finished = Number(job.finished_points || 0);
  return finished > 0 && Number(job.error_points || 0) >= finished;
}

function jobStatusLabel(job) {
  if (job.finalizing) return ["building report", "active"];
  if (job.status === "completed" && jobAllErrored(job)) return ["no results", "failed"];
  if (job.status === "completed" && Number(job.error_points || 0) > 0) return ["completed with errors", "defined"];
  return [job.status, statusClass(job.status)];
}

function jobErrorNote(job) {
  const errored = Number(job.error_points || 0);
  const reason = job.point_error || job.error;
  if (!errored && !job.error) return null;
  const note = document.createElement("span");
  note.className = "job-error";
  note.textContent = errored
    ? `${errored} point${errored === 1 ? "" : "s"} did not simulate${reason ? `: ${reason}` : "."}`
    : reason;
  return note;
}

function jobDisplayName(job) {
  const title = job.study_title || "";
  const name = job.experiment_name || job.name || "";
  if (title && name) return `${title} · ${name}`;
  return title || name || "Experiment";
}

let jobPollFailures = 0;
let jobPollError = "";
const JOB_POLL_MAX_FAILURES = 6;

function jobPollProblem() {
  if (!jobPollError) return null;
  const row = document.createElement("div");
  row.className = "poll-problem";
  row.setAttribute("role", "alert");
  const text = document.createElement("span");
  text.textContent = jobPollFailures >= JOB_POLL_MAX_FAILURES
    ? `Status unavailable: ${jobPollError}`
    : `Status unavailable, retrying: ${jobPollError}`;
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "compact-button";
  retry.textContent = "Retry";
  retry.addEventListener("click", () => {
    jobPollFailures = 0;
    scheduleJobPoll(0);
  });
  row.append(text, retry);
  return row;
}

function renderTrackedJobs() {
  const container = byId("launch-result");
  const jobs = visibleTrackedJobs();
  if (jobs.length === 0) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const title = document.createElement("h3");
  title.textContent = "Durable local execution";
  const cards = jobs.map((job) => {
    const card = document.createElement("div");
    card.className = "tracked-job";
    const heading = document.createElement("div");
    const identity = document.createElement("strong");
    identity.textContent = String(job.name || "experiment").toUpperCase();
    const status = document.createElement("span");
    const [statusText, statusTone] = jobStatusLabel(job);
    status.className = `job-status ${statusTone}`;
    status.textContent = statusText;
    heading.append(identity, status);
    const progress = document.createElement("div");
    progress.className = "progress-track";
    const bar = document.createElement("span");
    const finished = Number(job.finished_points || 0);
    const total = Number(job.point_count || 0);
    bar.style.width = `${total ? Math.min(100, finished / total * 100) : 0}%`;
    progress.append(bar);
    const detail = document.createElement("small");
    detail.textContent = jobPointSummary(job);
    const id = document.createElement("code");
    id.textContent = job.experiment_id;
    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (["queued", "running", "cancelling"].includes(job.status)) {
      actions.append(jobActionButton("Cancel", async () => {
        trackJob(job.experiment_id, await mutateJob(job.experiment_id, "cancel"));
        renderTrackedJobs();
        scheduleJobPoll(250);
      }, job.experiment_id));
    }
    if (job.status === "cancelled") {
      actions.append(jobActionButton("Resume unfinished", async () => {
        trackJob(job.experiment_id, await mutateJob(job.experiment_id, "resume"));
        renderTrackedJobs();
        scheduleJobPoll(250);
      }, job.experiment_id));
    }
    if (job.report_url) actions.append(reportLink(job.report_url));
    if (["completed", "failed", "cancelled"].includes(job.status) && !jobAllErrored(job)) {
      actions.append(waveformButton(job.experiment_id));
    }
    const actionError = jobActionError(job.experiment_id);
    if (actionError) actions.append(actionError);
    const errorNote = jobErrorNote(job);
    if (errorNote) actions.append(errorNote);
    if (job.postprocess_error) {
      const error = document.createElement("span");
      error.className = "job-error";
      error.textContent = job.postprocess_error;
      actions.append(error);
    }
    card.append(heading, progress, detail, id, actions);
    return card;
  });
  const problem = jobPollProblem();
  container.replaceChildren(title, ...(problem ? [problem] : []), ...cards);
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

// Polls every second while a job is live. A failed read backs off (1s, 2s,
// 4s ... capped at 30s) and after JOB_POLL_MAX_FAILURES in a row stops,
// leaving an inline "Status unavailable -- Retry" instead of hammering a
// server that is down or a job directory that is gone.
async function pollTrackedJobs() {
  jobPollTimer = null;
  const outcomes = await Promise.allSettled([...trackedJobs.values()].map(refreshTrackedJob));
  const failure = outcomes.find((outcome) => outcome.status === "rejected");
  if (failure) {
    jobPollFailures += 1;
    jobPollError = failure.reason?.message || "the server did not answer";
  } else {
    jobPollFailures = 0;
    jobPollError = "";
  }
  renderTrackedJobs();
  settleSensitivityJob();
  if (!failure) await loadHistory(false);
  renderHistoryJobPollProblem();
  if (![...trackedJobs.values()].some(isActiveJob)) return;
  if (jobPollFailures >= JOB_POLL_MAX_FAILURES) return;
  scheduleJobPoll(failure ? Math.min(30000, 1000 * 2 ** jobPollFailures) : 1000);
}

function renderHistoryJobPollProblem() {
  const slot = byId("history-poll-problem");
  const problem = jobPollProblem();
  slot.replaceChildren(...(problem ? [problem] : []));
  slot.hidden = !problem;
}

function scheduleJobPoll(delay = 1000) {
  window.clearTimeout(jobPollTimer);
  jobPollTimer = window.setTimeout(pollTrackedJobs, delay);
}

let latestHistory = null;

function renderHistory(result) {
  latestHistory = result;
  byId("history-total").textContent = result.summary.total_jobs.toLocaleString();
  byId("history-active").textContent = result.summary.active_jobs.toLocaleString();
  byId("history-reports").textContent = result.summary.reports.toLocaleString();
  const index = byId("history-index");
  index.textContent = result.index.current ? "Current" : (result.index.available ? "Stale" : "Unavailable");
  index.className = result.index.current ? "index-ready" : "index-missing";
  index.title = result.index.message;

  const compare = byId("open-compare");
  const completed = comparableJobs().length;
  compare.disabled = completed < 2;
  compare.title = completed < 2
    ? `Needs two completed runs to compare (${completed} so far).`
    : "Diff two finished runs";

  const jobs = result.jobs.filter(matchesHistoryFilter).map((job) => {
    const row = document.createElement("div");
    row.className = "history-item";
    const top = document.createElement("div");
    top.className = "history-item-top";
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    const meta = document.createElement("small");
    const kind = job.statistical ? "Statistical experiment" : "Experiment";
    title.textContent = job.study_title || job.experiment_name ? jobDisplayName(job) : kind;
    meta.textContent = `${relativeTime(job.recorded_at)} · ${job.study_title || job.experiment_name ? `${kind.toLowerCase()} · ` : ""}${job.execution_mode} execution`;
    identity.append(title, meta);
    const status = document.createElement("span");
    const [statusText, statusTone] = jobStatusLabel(job);
    status.className = `job-status ${statusTone}`;
    status.textContent = statusText;
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
    details.textContent = jobPointSummary(job);
    bottom.append(details);
    if (job.report_url) bottom.append(reportLink(job.report_url));
    if (!jobAllErrored(job)) bottom.append(waveformButton(job.experiment_id));
    if (job.status === "completed" && !jobAllErrored(job)) {
      // Sensitivity perturbs a sampled point; a boundary brackets any two
      // points that differ in one variable, which sweeps produce best.
      if (job.statistical) bottom.append(sensitivityButton(job.experiment_id));
      bottom.append(boundaryButton(job.experiment_id));
    }
    if (["queued", "running", "cancelling"].includes(job.status)) {
      bottom.append(jobActionButton("Cancel", async () => {
        trackJob(job.experiment_id, {name: "recovered", ...job, ...await mutateJob(job.experiment_id, "cancel")});
        renderTrackedJobs();
        scheduleJobPoll(250);
      }, job.experiment_id));
    } else if (job.status === "cancelled") {
      bottom.append(jobActionButton("Resume unfinished", async () => {
        trackJob(job.experiment_id, {name: "resumed", ...job, ...await mutateJob(job.experiment_id, "resume")});
        renderTrackedJobs();
        scheduleJobPoll(250);
      }, job.experiment_id));
    } else if (job.status === "completed" && !job.report_url) {
      bottom.append(jobActionButton("Build report", async () => {
        await mutateJob(job.experiment_id, "finalize");
        await loadHistory();
      }, job.experiment_id));
    }
    const actionError = jobActionError(job.experiment_id);
    if (actionError) bottom.append(actionError);

    const id = document.createElement("code");
    id.textContent = job.experiment_id;
    row.append(top, progress, bottom);
    const errorNote = jobErrorNote(job);
    if (errorNote) row.append(errorNote);
    row.append(id);
    return row;
  });
  byId("job-history").replaceChildren(...(jobs.length ? jobs : [emptyHistory("No durable experiments found.")]));
  for (const job of result.jobs.filter((item) => ["queued", "running", "cancelling"].includes(item.status))) {
    if (!trackedJobs.has(job.experiment_id)) trackJob(job.experiment_id, {name: job.experiment_name || "recovered", ...job});
  }
  adoptRecoveredJobs();
  if ([...trackedJobs.values()].some((job) => ["queued", "running", "cancelling"].includes(job.status))) {
    renderTrackedJobs();
    if (!jobPollTimer && jobPollFailures < JOB_POLL_MAX_FAILURES) scheduleJobPoll();
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
    const limit = byId("history-limit").value || "12";
    const response = await fetch(`/api/history?limit=${encodeURIComponent(limit)}`);
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
  const sequence = ++freezeSequence;
  const previewed = latestPreview;
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
        expected_recipe_sha256: previewed.recipe.sha256,
        expected_plan_id: previewed.plan.plan_id,
      }),
    });
    const result = await response.json();
    if (sequence !== freezeSequence) return;
    if (!response.ok) {
      if (result.valid === false) {
        renderPreview(result);
        return;
      }
      throw new Error(result.error?.message || "Plan could not be frozen");
    }
    frozenLaunch = {...result, recipe_sha256: previewed.recipe.sha256};
    byId("frozen-plan-id").textContent = result.plan.plan_id;
    byId("confirm-points").textContent = result.plan.point_count.toLocaleString();
    byId("confirm-experiments").textContent = result.execution.experiment_count.toLocaleString();
    byId("confirm-runs").textContent = result.execution.total_run_count.toLocaleString();
    byId("confirm-concurrency").textContent = result.execution.max_concurrency.toLocaleString();
    byId("frozen-artifact").textContent = result.plan.artifact;
    byId("execution-acknowledgement").checked = false;
    byId("start-button").disabled = true;
    byId("start-button").textContent = "Start local study";
    byId("execution-confirmation").hidden = false;
    byId("remote-preview-controls").hidden = false;
    byId("remote-preview-result").hidden = true;
    byId("remote-preview-button").disabled = false;
    byId("launch-result").hidden = true;
    renderErrors([]);
  } catch (error) {
    if (sequence !== freezeSequence) return;
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
  const response = await fetch("/api/remote/jobs");
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || "Remote jobs could not be read");
  remoteJobs = new Map((result.jobs || []).map((job) => [job.remote_job_id, job]));
  renderRemoteJobs();
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
    trackJob(experiment.experiment_id, {
      ...experiment,
      study_title: studyTitle(),
      experiment_name: experiment.name,
      finished_points: 0,
      passed_points: 0,
      failed_points: 0,
      error_points: 0,
      report_available: false,
    }, studyProjectKey());
  }
  jobPollFailures = 0;
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
    syncStartButton("start-button");
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
  const dirty = project.kind === "optimization" ? optimizationDirty : studyDirty || hasUnsavedNetlistEdits();
  const kind = project.kind === "optimization" ? "optimization recipe" : "study recipe and netlists";
  if (!confirmDiscard(dirty, `Opening "${project.name}" will discard unsaved changes to the current ${kind}. Continue?`)) return;
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(project.slug)}/recipe`);
    const loaded = await response.json();
    if (!response.ok) throw new Error(loaded.error?.message || "Recipe could not be loaded");
    if (project.kind === "optimization") {
      setCurrentOptimizationProject(project.slug, project.path);
      optimizationRecipe = loaded;
      invalidateOptimizationLaunch();
      displayedOptimizationStudy = null;
      selectedQualificationSource = null;
      latestQualificationPreview = null;
      frozenQualificationLaunch = null;
      byId("optimization-results").hidden = true;
      optimizationDisplayUnits = new WeakMap();
      clearOptimizationJob();
      renderOptimizationEditors();
      await previewOptimization();
      recoverOptimizationJob().catch(() => {});
      showView("optimization");
    } else {
      setCurrentStudyProject(project.slug, project.path);
      recipe = loaded;
      netlistEdits.clear();
      forgetFinishedJobs();
      adoptRecoveredJobs();
      variableDisplayUnits = new WeakMap();
      cornerDisplayUnits = new WeakMap();
      invalidateFrozenPlan();
      populateRecipeControls();
      // Re-scopes the schematic-source dropdown's bare filenames to the
      // newly opened project, not whichever project was open at bootstrap.
      await Promise.all([loadNetlistFiles(), loadSchematicFiles()]);
      await preview();
      showView("definition");
    }
  } catch (error) {
    renderProjectsError(`${project.name}: ${error.message}`);
  }
}

// The editor keeps the deleted project's recipe open (it may be the only
// copy left), but it is no longer attached to a folder: Save falls back to a
// download instead of writing to a project that is gone.
function detachDeletedProject(project) {
  const warning = (statusId, dirtySetter) => {
    markDirty(statusId, dirtySetter);
    byId(statusId).textContent = `"${project.name}" was deleted. This recipe is only in the browser now; Save recipe downloads it.`;
  };
  if (project.slug === currentStudyProjectSlug) {
    setCurrentStudyProject(null, null);
    warning("save-status", (v) => { studyDirty = v; });
    renderProjectsError(`"${project.name}" was open in Study setup. Its recipe stays in the editor, detached from the deleted folder.`);
  }
  if (project.slug === currentOptimizationProjectSlug) {
    setCurrentOptimizationProject(null, null);
    warning("optimization-save-status", (v) => { optimizationDirty = v; });
    renderProjectsError(`"${project.name}" was open in Optimization. Its recipe stays in the editor, detached from the deleted folder.`);
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

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "destructive-button";
      deleteButton.textContent = "Delete";
      let confirmTimer = null;
      deleteButton.addEventListener("click", async () => {
        if (!deleteButton.classList.contains("confirming")) {
          deleteButton.classList.add("confirming");
          deleteButton.textContent = "Really delete?";
          confirmTimer = window.setTimeout(() => {
            deleteButton.classList.remove("confirming");
            deleteButton.textContent = "Delete";
          }, 4000);
          return;
        }
        window.clearTimeout(confirmTimer);
        deleteButton.disabled = true;
        deleteButton.textContent = "Deleting…";
        try {
          const response = await fetch(`/api/projects/${encodeURIComponent(project.slug)}`, {
            method: "DELETE",
            headers: {"X-LTspice-System-Builder": "1"},
          });
          if (!response.ok) {
            const result = await response.json().catch(() => ({}));
            throw new Error(result.error?.message || "Project could not be deleted");
          }
          renderProjectsError(null);
          detachDeletedProject(project);
          await loadProjects();
        } catch (error) {
          renderProjectsError(`${project.name}: ${error.message}`);
          deleteButton.disabled = false;
          deleteButton.classList.remove("confirming");
          deleteButton.textContent = "Delete";
        }
      });

      buttonRow.append(openButton, deleteButton);

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
  const sessionResponse = await fetch("/api/session");
  if (!sessionResponse.ok) throw new Error("Local session could not be established");
  const session = await sessionResponse.json();
  variableDisplayUnits = new WeakMap();
  cornerDisplayUnits = new WeakMap();
  invalidateFrozenPlan();
  byId("workspace").textContent = session.workspace;
  byId("workspace").title = session.workspace;
  byId("projects-workspace").textContent = session.workspace;
  // Each loader reports its own failure; one failing (say the remote job
  // list) must not stop the rest, and the message goes to a banner every
  // view shows rather than into the Study setup panel.
  const loaders = [
    ["Metric definitions", loadMetricSchema],
    ["Schematic files", loadSchematicFiles],
    ["Netlist files", loadNetlistFiles],
    ["Workspace history", loadHistory],
    ["Remote jobs", loadRemoteJobs],
    ["Projects", loadProjects],
    ["LTspice status", loadLtspiceStatus],
  ];
  const outcomes = await Promise.allSettled(loaders.map(([, load]) => load()));
  renderAppErrors(outcomes
    .map((outcome, index) => [loaders[index][0], outcome])
    .filter(([, outcome]) => outcome.status === "rejected")
    .map(([label, outcome]) => `${label}: ${outcome.reason?.message || outcome.reason}`));
}

function renderAppErrors(messages) {
  const container = byId("app-errors");
  if (!messages.length) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const title = document.createElement("strong");
  title.textContent = "Some of this workspace could not be loaded";
  const list = document.createElement("ul");
  for (const message of messages) {
    const item = document.createElement("li");
    item.textContent = message;
    list.append(item);
  }
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "compact-button";
  retry.textContent = "Reload";
  retry.addEventListener("click", () => window.location.reload());
  container.replaceChildren(title, list, retry);
  container.hidden = false;
}

async function loadLtspiceStatus() {
  try {
    const response = await fetch("/api/settings/ltspice");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "LTspice status could not be read");
    renderLtspiceStatus(result);
  } catch (error) {
    const errorEl = byId("ltspice-settings-error");
    errorEl.textContent = error.message;
    errorEl.hidden = false;
    throw error;
  }
}

// Every control that launches LTspice (Simulate once, the three Start
// buttons) carries data-needs-ltspice. While the executable is missing they
// stay disabled with the reason shown beside them, instead of launching a
// job whose every point errors with "LTspice executable not found".
let ltspiceMissing = false;
let ltspiceMissingReason = "";

function ltspiceGateNote() {
  const note = document.createElement("span");
  note.className = "field-problem ltspice-gate-note";
  note.hidden = !ltspiceMissing;
  note.textContent = ltspiceMissingReason;
  return note;
}

const START_GATES = [
  ["start-button", "execution-acknowledgement"],
  ["optimization-start", "optimization-acknowledgement"],
  ["qualification-start", "qualification-acknowledgement"],
];

function syncStartButton(buttonId) {
  const gate = START_GATES.find(([id]) => id === buttonId);
  if (!gate) return;
  const acknowledgement = byId(gate[1]);
  byId(buttonId).disabled = ltspiceMissing || !acknowledgement.checked || acknowledgement.disabled;
}

function applyLtspiceGate() {
  document.querySelectorAll("[data-needs-ltspice]").forEach((button) => {
    if (START_GATES.some(([id]) => id === button.id)) {
      syncStartButton(button.id);
    } else if (ltspiceMissing) {
      if (!button.disabled) button.dataset.ltspiceBlocked = "true";
      button.disabled = true;
    } else if (button.dataset.ltspiceBlocked) {
      delete button.dataset.ltspiceBlocked;
      button.disabled = false;
    }
  });
  document.querySelectorAll(".ltspice-gate-note").forEach((note) => {
    note.hidden = !ltspiceMissing;
    note.textContent = ltspiceMissingReason;
  });
}

function renderLtspiceStatus(status) {
  ltspiceMissing = !status.exists;
  ltspiceMissingReason = ltspiceMissing
    ? `LTspice was not found (${status.executable}). Set its location on the Dashboard to simulate.`
    : "";
  applyLtspiceGate();
  byId("ltspice-path").textContent = status.executable;
  byId("ltspice-path").title = status.executable;
  byId("ltspice-path-input").value = "";
  byId("ltspice-path-input").placeholder = status.executable;
  const pill = byId("ltspice-status-pill");
  const sourceLabel = {
    environment: "env var",
    configured: "configured",
    discovered: "auto-detected",
  }[status.source] || status.source;
  if (status.exists) {
    pill.className = "status-pill valid";
    pill.textContent = `Found · ${sourceLabel}`;
  } else {
    pill.className = "status-pill invalid";
    pill.textContent = "Not found";
  }
}

async function saveLtspiceExecutable(executable) {
  const errorEl = byId("ltspice-settings-error");
  errorEl.hidden = true;
  try {
    const response = await fetch("/api/settings/ltspice", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({executable}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "LTspice setting could not be saved");
    renderLtspiceStatus(result);
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
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
  syncStartButton("start-button");
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
for (const id of ["sample-count", "seed", "sampling-method", "max-concurrency"]) {
  byId(id).addEventListener("input", schedulePreview);
}
byId("reuse-cache").addEventListener("change", schedulePreview);
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
  if (!confirmDiscard(studyDirty || hasUnsavedNetlistEdits(), "Loading a different recipe will discard unsaved changes to this one and its netlists. Continue?")) {
    event.target.value = "";
    return;
  }
  try {
    recipe = JSON.parse(await file.text());
    netlistEdits.clear();
    forgetFinishedJobs();
    // This file has nothing to do with whatever project (if any) was open
    // before -- without clearing this, Save would silently write the newly
    // loaded recipe into the previous project, and an unrelated netlist
    // Import would land in its folder too.
    setCurrentStudyProject(null, null);
    byId("save-status").textContent = "";
    variableDisplayUnits = new WeakMap();
    cornerDisplayUnits = new WeakMap();
    invalidateFrozenPlan();
    populateRecipeControls();
    // Re-scopes the schematic-source dropdown back to full paths now that no
    // project is open to shorten filenames against.
    await Promise.all([loadNetlistFiles(), loadSchematicFiles()]);
    await preview();
  } catch (error) {
    renderPreview({valid: false, errors: [{path: "$", message: `Could not load recipe: ${error.message}`}]});
  }
  event.target.value = "";
});
byId("save-button").addEventListener("click", async () => {
  if (!recipe) return;
  updateRecipeFromControls();
  const status = byId("save-status");
  if (!currentStudyProjectSlug) {
    const blob = new Blob([`${JSON.stringify(recipe, null, 2)}\n`], {type: "application/json"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${(recipe.name || "study").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "study"}.ltstudy.json`;
    link.click();
    URL.revokeObjectURL(link.href);
    markClean("save-status", (v) => { studyDirty = v; });
    return;
  }
  const button = byId("save-button");
  button.disabled = true;
  status.classList.remove("is-error");
  status.textContent = "Saving…";
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(currentStudyProjectSlug)}/recipe`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify(recipe),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Recipe could not be saved");
    studyDirty = false;
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
byId("refresh-history").addEventListener("click", loadHistory);
byId("refresh-projects").addEventListener("click", loadProjects);
byId("refresh-netlists").addEventListener("click", async () => {
  const button = byId("refresh-netlists");
  button.disabled = true;
  try {
    await loadNetlistFiles();
    requestPreview();
  } catch (error) {
    renderScopedErrors([{path: "experiments", message: error.message}]);
  } finally {
    button.disabled = false;
  }
});
byId("ltspice-settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("ltspice-path-input");
  const value = input.value.trim();
  if (!value) return;
  saveLtspiceExecutable(value);
});
byId("ltspice-auto-detect").addEventListener("click", () => saveLtspiceExecutable(null));
byId("netlist-import-input").addEventListener("change", async (event) => {
  const input = event.target;
  const file = input.files[0];
  input.value = ""; // allow re-selecting the same filename later
  if (!file) return;
  const lowerName = file.name.toLowerCase();
  if (!lowerName.endsWith(".cir") && !lowerName.endsWith(".net")) {
    renderScopedErrors([{path: "experiments", message: "Import requires a .cir or .net file."}]);
    return;
  }
  try {
    const content = await file.text();
    const destination = currentStudyProjectPath ? `${currentStudyProjectPath}/${file.name}` : file.name;
    const response = await fetch(`/api/recipe/netlist?path=${encodeURIComponent(destination)}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-LTspice-System-Builder": "1",
      },
      body: JSON.stringify({content}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "Netlist could not be imported");
    renderScopedErrors([]);
    await loadNetlistFiles();
  } catch (error) {
    renderScopedErrors([{path: "experiments", message: error.message}]);
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
byId("theme-select").addEventListener("change", (event) => {
  const theme = event.target.value;
  applyTheme(theme);
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch (_) {
    // Theme switching still works for this page when storage is disabled.
  }
});

loadInitialState().catch((error) => {
  renderAppErrors([error.message]);
});
