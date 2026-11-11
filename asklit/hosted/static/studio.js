/* Same-origin editor. All generated text uses textContent, never innerHTML. */
"use strict";
const $ = (id) => document.getElementById(id);
let me,
  bot,
  profileIndex = 0,
  history = [],
  previewAttachments = [],
  previewUploadBusy = false,
  previewGeneration = 0,
  draftDocuments = [],
  dirty = false,
  polling,
  resourcesKey = "",
  pinnedEvaluation = null,
  jobBusy = false;
const lines = (value) =>
  value
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
let noticeTimer;
function notice(text, error = false, persistent = false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = text;
  $("notice").className = error ? "error" : "";
  if (text && !error && !persistent)
    noticeTimer = setTimeout(() => notice(""), 4500);
}
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function action(text, fn, cls = "secondary") {
  const b = el("button", text, cls);
  b.type = "button";
  b.onclick = () => perform(b, fn);
  return b;
}
async function perform(button, fn) {
  if (button) button.disabled = true;
  try {
    await fn();
  } catch (e) {
    notice(e.message, true);
  } finally {
    if (button) button.disabled = false;
    if (bot) {
      updateRunButton();
      $("export-bundle").disabled = jobBusy;
    }
  }
}
async function api(path, method = "GET", data) {
  const headers = {};
  if (me) headers["X-CSRF-Token"] = me.csrf;
  if (data !== undefined && !(data instanceof FormData))
    headers["Content-Type"] = "application/json";
  const r = await fetch("/api" + path, {
    method,
    headers,
    body:
      data === undefined
        ? undefined
        : data instanceof FormData
          ? data
          : JSON.stringify(data),
  });
  if (!r.ok) {
    let message = `Request failed (${r.status}).`;
    try {
      const body = await r.json();
      message =
        typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail);
    } catch {}
    const error = new Error(message);
    error.status = r.status;
    throw error;
  }
  return r.headers.get("content-type")?.includes("application/json")
    ? r.json()
    : null;
}
function show(section) {
  notice("");
  document.querySelectorAll(".menu").forEach((menu) => (menu.open = false));
  for (const id of ["login", "dashboard", "organization", "editor"])
    $(id).hidden = id !== section;
}
function markDirty() {
  dirty = true;
  $("save-status").textContent = "Unsaved changes";
}
window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
function option(select, value, text) {
  const o = el("option", text);
  o.value = value;
  select.append(o);
}
function bind(id, fn) {
  $(id).onclick = (e) => perform(e.currentTarget, fn);
}
function readProfile() {
  if (!bot) return;
  const p = bot.config.profiles[profileIndex];
  const oldKey = p.key;
  if (oldKey !== $("profile-key").value) {
    for (const id of ["evaluation-prompts", "evaluation-sources"]) {
      $(id)
        .querySelectorAll("input")
        .forEach((input) => {
          if (input.value === oldKey) input.value = $("profile-key").value;
        });
    }
    bot.config.evaluation = readEvaluation();
  }
  Object.assign(p, {
    key: $("profile-key").value,
    label: $("profile-label").value,
    knowledgebase: $("profile-kb").value,
    prompt: $("prompt").value,
    conversation_starters: lines($("starters").value),
    connected_files: lines($("connected-files").value),
  });
}
function renderProfile() {
  const p = bot.config.profiles[profileIndex];
  $("profile").replaceChildren();
  bot.config.profiles.forEach((p, i) => option($("profile"), i, p.label));
  $("profile").value = String(profileIndex);
  for (const [id, key] of [
    ["profile-key", "key"],
    ["profile-label", "label"],
    ["profile-kb", "knowledgebase"],
    ["prompt", "prompt"],
  ])
    $(id).value = p[key];
  $("starters").value = p.conversation_starters.join("\n");
  $("connected-files").value = p.connected_files.join("\n");
  $("upload-kb").value = p.knowledgebase;
  $("preview-profile").textContent = p.label;
  knowledgeHint();
}
function knowledgeHint() {
  const files = lines($("connected-files").value);
  const docs = draftDocuments.filter(
    (d) =>
      d.status === "ready" &&
      d.knowledgebase === $("profile-kb").value &&
      (!files.length || files.includes(d.filename)),
  );
  $("knowledge-hint").textContent = docs.length
    ? `${docs.length} source${docs.length === 1 ? "" : "s"} connected`
    : "No sources connected";
}
$("profile-kb").addEventListener("input", () => {
  $("upload-kb").value = $("profile-kb").value;
  knowledgeHint();
});
$("connected-files").addEventListener("input", knowledgeHint);
function readConfig() {
  readProfile();
  Object.assign(bot.config, {
    title: $("title").value,
    welcome: $("welcome").value,
    description: $("description").value,
    footer: $("footer").value,
    model_preset: $("model").value,
    top_k: Number($("top-k").value),
    uploads_enabled: $("uploads-enabled").checked,
    upload_prompt: $("upload-prompt").value,
    rubrics: lines($("rubrics").value),
    evaluation: readEvaluation(),
  });
}
function revealInvalid(input) {
  const tab = input.closest(".tab");
  if (tab) selectTab(tab.id.replace("tab-", ""));
  for (
    let parent = input.parentElement;
    parent;
    parent = parent.parentElement
  ) {
    if (parent.tagName === "DETAILS") parent.open = true;
  }
}
$("editor").addEventListener(
  "invalid",
  (event) => revealInvalid(event.target),
  true,
);
async function save() {
  const invalid = [
    ...document.querySelectorAll(
      "#editor input, #editor textarea, #editor select",
    ),
  ].find(
    (input) =>
      ![
        "question",
        "upload-file",
        "upload-kb",
        "workspace-import",
        "owner-id",
      ].includes(input.id) &&
      !input.id.startsWith("sharing-") &&
      !input.id.startsWith("password-") &&
      !input.validity.valid,
  );
  if (invalid) {
    revealInvalid(invalid);
    invalid.reportValidity();
    throw new Error("Check the highlighted field.");
  }
  readConfig();
  bot = await api(`/bots/${bot.id}`, "PUT", {
    version: bot.version,
    config: bot.config,
  });
  dirty = false;
  $("bot-heading").textContent = bot.title;
  $("save-status").textContent = "Saved";
  renderEvaluationOptions();
  $("preview-attach").hidden = !bot.config.uploads_enabled;
  if (!bot.config.uploads_enabled) {
    previewAttachments = [];
    renderPreviewFiles();
  }
  updateRunButton();
}
async function dashboard() {
  clearInterval(polling);
  bot = null;
  dirty = false;
  show("dashboard");
  const bots = await api("/bots");
  const org = $("org").value;
  $("bots").replaceChildren();
  for (const b of bots.filter((b) => b.org_id === org)) {
    const card = action("", () => openBot(b.id), "card");
    card.append(
      el("h2", b.title),
      el("p", b.published_id ? "Published" : "Draft", "muted"),
    );
    $("bots").append(card);
  }
  if (!$("bots").children.length)
    $("bots").append(el("p", "Your first AskLit starts here."));
  $("manage-org").hidden =
    me.organizations.find((o) => o.id === org)?.role === "member";
}
async function refreshMe() {
  const selected = $("org").value;
  me = await api("/me");
  $("identity").textContent = me.name;
  $("logout").hidden = false;
  $("org").replaceChildren();
  me.organizations.forEach((o) => option($("org"), o.id, o.name));
  if (me.organizations.some((o) => o.id === selected))
    $("org").value = selected;
}
async function openBot(id) {
  if (dirty && !confirm("Leave this draft without saving your changes?"))
    return;
  clearInterval(polling);
  bot = await api(`/bots/${id}`);
  profileIndex = 0;
  resourcesKey = "";
  pinnedEvaluation = null;
  jobBusy = false;
  history = [];
  previewGeneration++;
  previewAttachments = [];
  renderPreviewFiles();
  dirty = false;
  $("transcript").replaceChildren();
  $("evaluation-results").replaceChildren();
  delete $("evaluation-results").dataset.job;
  $("bot-heading").textContent = bot.title;
  $("save-status").textContent = "Saved";
  for (const id of ["title", "description", "welcome", "footer"])
    $(id).value = bot.config[id] || "";
  $("model").replaceChildren();
  me.models.forEach((m) => option($("model"), m, m));
  $("model").value = bot.config.model_preset;
  $("top-k").value = bot.config.top_k;
  $("uploads-enabled").checked = !!bot.config.uploads_enabled;
  $("upload-prompt").value = bot.config.upload_prompt || "";
  $("preview-attach").hidden = !bot.config.uploads_enabled;
  $("rubrics").value = bot.config.rubrics.join("\n");
  $("workspace-download").href = `/api/bots/${bot.id}/workspace`;
  $("owner-id").value = "";
  renderProfile();
  renderScenarios();
  show("editor");
  selectTab("knowledge");
  await refreshResources();
  await loadSharing();
  polling = setInterval(
    () => refreshResources().catch((e) => notice(e.message, true)),
    4000,
  );
}
function selectTab(tab) {
  notice("");
  document
    .querySelectorAll(".tab")
    .forEach((e) => (e.hidden = e.id !== "tab-" + tab));
  document
    .querySelectorAll("[data-tab]")
    .forEach((e) =>
      e.setAttribute("aria-current", String(e.dataset.tab === tab)),
    );
}
document
  .querySelectorAll("[data-tab]")
  .forEach((e) => (e.onclick = () => selectTab(e.dataset.tab)));
document.querySelectorAll(".back").forEach(
  (e) =>
    (e.onclick = () =>
      perform(e, async () => {
        if (!dirty || confirm("Leave without saving?")) await dashboard();
      })),
);
document
  .querySelectorAll("#editor input, #editor textarea, #editor select")
  .forEach((e) =>
    e.addEventListener("input", () => {
      if (
        !e.id.startsWith("sharing-") &&
        !e.id.startsWith("password-") &&
        ![
          "generation-profile",
          "generation-model",
          "generation-count",
          "scenario-import",
          "profile",
          "question",
          "upload-file",
          "upload-kb",
          "workspace-import",
          "owner-id",
        ].includes(e.id)
      )
        markDirty();
    }),
  );
$("profile").onchange = () => {
  readProfile();
  profileIndex = Number($("profile").value);
  renderProfile();
  history = [];
  previewGeneration++;
  previewAttachments = [];
  renderPreviewFiles();
  $("transcript").replaceChildren();
};
bind("save", save);
let nameOperation,
  nameBusy = false;
function nameDialog({
  title,
  submit,
  value = "",
  pattern = "",
  hint = "",
  onSubmit,
}) {
  $("name-dialog-title").textContent = title;
  $("name-submit").textContent = submit;
  $("name-input").value = value;
  $("name-input").setCustomValidity("");
  if (pattern) $("name-input").setAttribute("pattern", pattern);
  else $("name-input").removeAttribute("pattern");
  $("name-hint").textContent = hint;
  $("name-hint").hidden = !hint;
  $("name-error").hidden = true;
  nameOperation = onSubmit;
  $("name-dialog").showModal();
  $("name-input").focus();
  $("name-input").select();
}
$("name-input").oninput = () => {
  $("name-input").setCustomValidity("");
  $("name-error").hidden = true;
};
$("name-cancel").onclick = () => $("name-dialog").close();
$("name-dialog").addEventListener("cancel", (event) => {
  if (nameBusy) event.preventDefault();
});
$("name-form").onsubmit = async (event) => {
  event.preventDefault();
  if (nameBusy) return;
  const name = $("name-input").value.trim();
  if (!name) {
    $("name-input").setCustomValidity("Enter a name.");
    $("name-input").reportValidity();
    return;
  }
  nameBusy = true;
  $("name-submit").disabled = true;
  $("name-cancel").disabled = true;
  $("name-input").readOnly = true;
  $("name-form").setAttribute("aria-busy", "true");
  try {
    await nameOperation(name);
    $("name-dialog").close();
    if (!$("editor").hidden) $("save").focus();
  } catch (error) {
    $("name-error").textContent = error.message;
    $("name-error").hidden = false;
  } finally {
    nameBusy = false;
    $("name-submit").disabled = false;
    $("name-cancel").disabled = false;
    $("name-input").readOnly = false;
    $("name-form").removeAttribute("aria-busy");
  }
};
bind("rename-kb", () => {
  const oldName = $("upload-kb").value;
  nameDialog({
    title: "Rename source group",
    submit: "Rename",
    value: oldName,
    pattern: "[a-z0-9-]{1,100}",
    hint: "Lowercase letters, numbers and hyphens.",
    onSubmit: async (newName) => {
      if (newName === oldName) return;
      await save();
      bot = await api(`/bots/${bot.id}/knowledgebases/rename`, "POST", {
        old_name: oldName,
        new_name: newName,
        version: bot.version,
      });
      renderProfile();
      $("upload-kb").value = newName;
      $("save-status").textContent = "Saved";
      notice("Source group renamed.");
      await refreshResources();
    },
  });
});
bind("add-profile", async () => {
  readProfile();
  const key = `prompt-${Date.now()}`;
  bot.config.profiles.push({
    key,
    label: "New prompt",
    knowledgebase: key,
    prompt: "You are a helpful assistant.",
    conversation_starters: [],
    connected_files: [],
  });
  profileIndex = bot.config.profiles.length - 1;
  history = [];
  previewGeneration++;
  previewAttachments = [];
  renderPreviewFiles();
  $("transcript").replaceChildren();
  renderScenarios();
  renderProfile();
  markDirty();
});
bind("remove-profile", async () => {
  if (bot.config.profiles.length === 1)
    throw new Error("Keep at least one prompt.");
  if (!confirm("Remove this prompt?")) return;
  history = [];
  previewGeneration++;
  previewAttachments = [];
  renderPreviewFiles();
  $("transcript").replaceChildren();
  bot.config.profiles.splice(profileIndex, 1);
  renderScenarios();
  profileIndex = 0;
  renderProfile();
  markDirty();
});
bind("new-bot", () =>
  nameDialog({
    title: "New AskLit",
    submit: "Create AskLit",
    onSubmit: async (title) => {
      const b = await api("/bots", "POST", { org_id: $("org").value, title });
      await openBot(b.id);
    },
  }),
);
bind("new-org", () =>
  nameDialog({
    title: "New organization",
    submit: "Create organization",
    onSubmit: async (name) => {
      const org = await api("/organizations", "POST", { name });
      await refreshMe();
      $("org").value = org.id;
      await dashboard();
    },
  }),
);
$("org").onchange = () => perform(null, dashboard);
bind("logout", async () => {
  await api("/logout", "POST");
  location.href = "/";
});
async function refreshResources() {
  if (!bot) return;
  const id = bot.id;
  const [docs, jobs, revisions] = await Promise.all([
    api(`/bots/${id}/documents`),
    api(`/bots/${id}/jobs`),
    api(`/bots/${id}/revisions`),
  ]);
  if (bot?.id !== id) return;
  const key = JSON.stringify([id, bot.published_id, docs, jobs, revisions]);
  if (key === resourcesKey) return;
  resourcesKey = key;
  $("documents").replaceChildren();
  draftDocuments = docs;
  knowledgeHint();
  for (const doc of docs) {
    const row = el("div", undefined, "row");
    row.append(
      el("span", doc.filename, "grow"),
      action("Remove", async () => {
        await save();
        await api(`/bots/${id}/documents/${doc.id}`, "DELETE");
        bot = await api(`/bots/${id}`);
        await refreshResources();
      }),
    );
    const state =
      doc.status === "ready"
        ? "Ready"
        : doc.status === "pending"
          ? "Preparing…"
          : doc.status === "indexing"
            ? "Preparing…"
            : doc.status;
    row.insertBefore(
      el(
        "span",
        doc.status === "failed" ? "Failed — remove and upload again" : state,
        doc.status === "failed" ? "error-text" : "muted",
      ),
      row.lastChild,
    );
    $("documents").append(row);
  }
  $("jobs").replaceChildren();
  $("task-panel").hidden = !jobs.length;
  const active = jobs.filter((j) => ["queued", "running"].includes(j.status));
  $("task-summary").textContent = active.length
    ? `Activity · ${active.length} in progress`
    : "Activity";
  jobBusy = jobs.some(
    (job) =>
      ["evaluate", "export"].includes(job.kind) &&
      ["queued", "running"].includes(job.status),
  );
  updateRunButton();
  $("export-bundle").disabled = jobBusy;
  const latestEvaluation = jobs.find((j) => j.kind === "evaluate");
  if (
    !pinnedEvaluation &&
    latestEvaluation?.status === "succeeded" &&
    $("evaluation-results").dataset.job !== latestEvaluation.id
  ) {
    renderResults(latestEvaluation.result.rows);
    $("evaluation-results").dataset.job = latestEvaluation.id;
  }
  const latestExport = jobs.find((j) => j.kind === "export");
  $("export-result").replaceChildren();
  if (latestExport?.status === "succeeded") {
    const link = el("a", "Download ZIP", "button secondary");
    link.href = `/api/bots/${id}/jobs/${latestExport.id}/download`;
    $("export-result").append(
      link,
      el(
        "span",
        `Exported ${new Date(latestExport.created * 1000).toLocaleString()}`,
        "muted",
      ),
    );
  } else if (latestExport) {
    $("export-result").textContent =
      latestExport.status === "failed"
        ? latestExport.result.error
        : "Preparing your download…";
  }
  const evaluationStatus = $("evaluation-status");
  evaluationStatus.textContent =
    latestEvaluation?.status === "failed"
      ? latestEvaluation.result.error
      : latestEvaluation &&
          ["queued", "running"].includes(latestEvaluation.status)
        ? `Evaluating… ${latestEvaluation.result.completed || 0} / ${latestEvaluation.result.total || "…"}`
        : "";
  evaluationStatus.className =
    latestEvaluation?.status === "failed" ? "error-text" : "muted";
  evaluationStatus.setAttribute(
    "role",
    latestEvaluation?.status === "failed" ? "alert" : "status",
  );
  for (const job of jobs) {
    const row = el("div", undefined, "row");
    row.append(
      el(
        "span",
        `${{ index: "Source preparation", evaluate: "Evaluation", export: "Streamlit export" }[job.kind] || job.kind} · ${{ succeeded: "Complete", queued: "Waiting", running: "In progress", failed: "Failed" }[job.status] || job.status}${job.result.error ? " — " + job.result.error : ""}`,
        "grow",
      ),
    );
    if (job.kind === "export" && job.status === "succeeded") {
      const a = el("a", "Download ZIP", "button secondary");
      a.href = `/api/bots/${id}/jobs/${job.id}/download`;
      row.append(a);
    }
    if (job.kind === "evaluate" && job.status === "succeeded")
      row.append(
        action("View results", async () => {
          pinnedEvaluation = job.id;
          renderResults(job.result.rows);
          selectTab("evaluate");
        }),
      );
    $("jobs").append(row);
  }
  $("publication").textContent = bot.published_id
    ? "Published"
    : "Private draft";
  $("copy-bot-id").hidden = !bot.published_id;
  $("unpublish").hidden = !bot.published_id;
  $("revision-history").hidden = !revisions.length;
  $("revisions").replaceChildren();
  for (const revision of revisions) {
    const row = el("div", undefined, "row");
    row.append(
      el("span", new Date(revision.created * 1000).toLocaleString(), "grow"),
      action(
        revision.id === bot.published_id
          ? "Current publication"
          : "Activate this version",
        async () => {
          await api(`/bots/${id}/revisions/${revision.id}/activate`, "POST");
          bot.published_id = revision.id;
          await refreshResources();
        },
      ),
    );
    if (revision.id === bot.published_id)
      row.lastChild.replaceWith(el("span", "Current", "muted"));
    $("revisions").append(row);
  }
}
$("upload").onsubmit = (e) => {
  e.preventDefault();
  perform(e.submitter, async () => {
    await save();
    const data = new FormData();
    data.append("file", $("upload-file").files[0]);
    data.append("knowledgebase", $("upload-kb").value);
    const result = await api(`/bots/${bot.id}/documents`, "POST", data);
    bot = await api(`/bots/${bot.id}`);
    notice(
      result.duplicate ? "That document is already uploaded." : "Source added.",
    );
    $("upload-file").value = "";
    await refreshResources();
  });
};
function appendMessage(role, text, sources = []) {
  const box = el("div", text, "message " + role);
  for (const source of sources) {
    const d = el("details");
    d.append(
      el("summary", `${source.filename}, page ${source.page}`),
      el("p", source.content),
    );
    box.append(d);
  }
  $("transcript").append(box);
  box.scrollIntoView({ block: "nearest" });
  return box;
}
$("preview").onsubmit = (e) => {
  e.preventDefault();
  if (previewUploadBusy) return;
  perform(e.submitter, async () => {
    if (history.length >= 24)
      throw new Error("Start a new preview after 12 questions.");
    await save();
    const question = $("question").value;
    const userMessage = appendMessage("user", question);
    const pending = appendMessage("assistant", "Thinking…");
    let result;
    try {
      result = await api(`/bots/${bot.id}/preview`, "POST", {
        message: question,
        profile: bot.config.profiles[profileIndex].key,
        history,
        attachments: previewAttachments,
      });
    } catch (error) {
      userMessage.remove();
      throw error;
    } finally {
      pending.remove();
    }
    appendMessage("assistant", result.answer, result.sources);
    history.push(
      { role: "user", content: question },
      { role: "assistant", content: result.answer },
    );
    $("question").value = "";
  });
};
bind("clear-preview", async () => {
  history = [];
  previewGeneration++;
  previewAttachments = [];
  renderPreviewFiles();
  $("transcript").replaceChildren();
});
function readEvaluation() {
  const selected = (id) =>
    [...$(id).querySelectorAll("input:checked")].map((e) => e.value);
  return {
    mode: $("evaluation-mode").value,
    prompts: selected("evaluation-prompts"),
    sources: selected("evaluation-sources"),
    models: selected("evaluation-models"),
    judge_model: $("evaluation-judge").value,
    top_k: Number($("evaluation-top-k").value),
  };
}
function renderEvaluationOptions() {
  const first = bot.config.profiles[0].key;
  const saved = bot.config.evaluation || {};
  const e = {
    mode: "single",
    prompts: [first],
    sources: [first],
    models: [bot.config.model_preset],
    judge_model: bot.config.model_preset,
    top_k: 5,
    ...saved,
  };
  $("evaluation-mode").value = e.mode;
  for (const [id, field, choices] of [
    [
      "evaluation-prompts",
      "prompts",
      bot.config.profiles.map((p) => [p.key, p.label]),
    ],
    [
      "evaluation-sources",
      "sources",
      bot.config.profiles.map((p) => [
        p.key,
        `${p.label} · ${p.knowledgebase}`,
      ]),
    ],
    [
      "evaluation-models",
      "models",
      me.models.map((m) => [m, me.model_labels?.[m] || m]),
    ],
  ]) {
    let selected = (e[field] || []).filter((key) =>
      choices.some(([v]) => v === key),
    );
    if (!selected.length) selected = [choices[0][0]];
    if (e.mode === "single") selected = selected.slice(0, 1);
    $(id).replaceChildren();
    for (const [key, title] of choices) {
      const label = el("label", undefined, "choice");
      const input = el("input");
      input.type = e.mode === "single" ? "radio" : "checkbox";
      input.name = id;
      input.value = key;
      input.checked = selected.includes(key);
      input.onchange = () => {
        bot.config.evaluation = readEvaluation();
        updateRunButton();
        markDirty();
      };
      label.append(input, el("span", title));
      $(id).append(label);
    }
  }
  for (const id of ["evaluation-judge", "generation-model"]) {
    const previous = $(id).value;
    $(id).replaceChildren();
    me.models.forEach((m) => option($(id), m, me.model_labels?.[m] || m));
    $(id).value =
      id === "evaluation-judge"
        ? e.judge_model || bot.config.model_preset
        : me.models.includes(previous)
          ? previous
          : bot.config.model_preset;
  }
  const generationProfile = $("generation-profile").value;
  $("generation-profile").replaceChildren();
  bot.config.profiles.forEach((p) =>
    option($("generation-profile"), p.key, p.label),
  );
  $("generation-profile").value = bot.config.profiles.some(
    (p) => p.key === generationProfile,
  )
    ? generationProfile
    : first;
  $("evaluation-top-k").value = e.top_k;
  bot.config.evaluation = readEvaluation();
}
function updateRunButton() {
  if (!bot) return;
  const e = readEvaluation();
  const cells = e.prompts.length * e.sources.length * e.models.length;
  const answers = cells * bot.config.scenarios.length;
  const shared = lines($("rubrics").value).length > 0;
  const judges =
    cells *
    bot.config.scenarios.filter(
      (s) =>
        shared ||
        /^(llm-rubric|model-rubric|rubric)\s*:\s*.+$/is.test(s.expected.trim()),
    ).length;
  const limit = me.evaluation_max_calls || 240;
  $("eval-cost").textContent =
    `${answers} answers${judges ? ` + ${judges} judge calls` : ""} · ${answers + judges} / ${limit} calls`;
  $("run-evaluation").disabled =
    jobBusy ||
    !answers ||
    answers + judges > limit ||
    bot.config.scenarios.some((s) => !s.input.trim());
}
$("evaluation-mode").onchange = () => {
  bot.config.evaluation = readEvaluation();
  renderEvaluationOptions();
  updateRunButton();
  markDirty();
};
for (const id of ["evaluation-judge", "evaluation-top-k", "rubrics"])
  $(id).addEventListener("input", () => {
    bot.config.evaluation = readEvaluation();
    updateRunButton();
  });
function renderScenarios() {
  renderEvaluationOptions();
  $("scenarios").replaceChildren();
  bot.config.scenarios.forEach((s, i) => {
    const row = el("div", undefined, "grid");
    const q = el("label", "Question");
    const input = el("textarea");
    input.value = s.input;
    input.rows = 2;
    input.maxLength = 8000;
    input.oninput = () => {
      s.input = input.value;
      updateRunButton();
      markDirty();
    };
    q.append(input);
    const a = el("label", "Expected answer");
    const expected = el("textarea");
    expected.value = s.expected;
    expected.rows = 2;
    expected.maxLength = 8000;
    expected.oninput = () => {
      s.expected = expected.value;
      updateRunButton();
      markDirty();
    };
    a.append(expected);
    row.append(
      q,
      a,
      action("Remove", async () => {
        bot.config.scenarios.splice(i, 1);
        renderScenarios();
        markDirty();
      }),
    );
    const extra = el("details");
    extra.append(el("summary", "Description & metadata"));
    const label = el("label", "Description");
    const description = el("input");
    description.value = s.description || "";
    description.maxLength = 2000;
    description.oninput = () => {
      s.description = description.value;
      markDirty();
    };
    label.append(description);
    extra.append(label);
    if (Object.keys(s.metadata || {}).length)
      extra.append(el("pre", JSON.stringify(s.metadata, null, 2)));
    $("scenarios").append(row, extra);
  });
  if (!bot.config.scenarios.length)
    $("scenarios").append(
      el(
        "p",
        "Add a question, import a CSV, or generate questions from your sources.",
        "muted",
      ),
    );
  updateRunButton();
}
bind("add-scenario", async () => {
  if (bot.config.scenarios.length >= 120)
    throw new Error("Keep at most 120 questions.");
  bot.config.scenarios.push({
    input: "",
    expected: "",
    description: "",
    metadata: {},
  });
  renderScenarios();
  markDirty();
});
$("scenario-import").onchange = () =>
  perform(null, async () => {
    const file = $("scenario-import").files[0];
    if (!file) return;
    try {
      if (file.size > 1000000) throw new Error("Use a CSV smaller than 1 MB.");
      if (
        bot.config.scenarios.length &&
        !confirm("Replace the current test questions with this CSV?")
      )
        return;
      const result = await api(`/bots/${bot.id}/scenarios/import`, "POST", {
        csv: await file.text(),
      });
      bot.config.scenarios = result.scenarios;
      renderScenarios();
      markDirty();
    } finally {
      $("scenario-import").value = "";
    }
  });
bind("scenario-export", async () => {
  await save();
  window.location.href = `/api/bots/${bot.id}/scenarios.csv`;
});
bind("generate-scenarios", async () => {
  const count = Number($("generation-count").value);
  if (!Number.isInteger(count) || count < 1 || count > 12)
    throw new Error("Choose 1–12 questions.");
  if (bot.config.scenarios.length + count > 120)
    throw new Error("Keep at most 120 questions.");
  await save();
  const id = bot.id;
  $("generation-status").textContent = "Generating…";
  try {
    const result = await api(`/bots/${id}/scenarios/generate`, "POST", {
      profile: $("generation-profile").value,
      model: $("generation-model").value,
      count,
    });
    if (bot?.id !== id) return;
    bot.config.scenarios.push(...result.scenarios);
    renderScenarios();
    markDirty();
    $("generation-status").textContent =
      `${result.scenarios.length} question${result.scenarios.length === 1 ? "" : "s"} added. Review before running.`;
  } finally {
    if (bot?.id === id && $("generation-status").textContent === "Generating…")
      $("generation-status").textContent = "";
  }
});
bind("run-evaluation", async () => {
  await save();
  await api(`/bots/${bot.id}/evaluate`, "POST");
  pinnedEvaluation = null;
  notice("Evaluation started.");
  await refreshResources();
});
function renderResults(rows) {
  const root = $("evaluation-results");
  const graded = rows.filter((r) => r.passed !== null);
  root.replaceChildren(
    el(
      "h3",
      `${rows.filter((r) => r.passed === true).length} of ${graded.length} graded answers passed`,
    ),
  );
  const groups = new Map();
  for (const row of rows) {
    const key = JSON.stringify([
      row.profile,
      row.source_label || row.knowledgebase || "",
      row.model || "",
    ]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const summary = el("table");
  const head = el("tr");
  ["Prompt", "Source group", "Model", "Passed", "Time"].forEach((v) =>
    head.append(el("th", v)),
  );
  summary.append(head);
  for (const [key, values] of groups) {
    const tr = el("tr");
    const [prompt, source, model] = JSON.parse(key);
    const passed = values.filter((v) => v.passed === true).length;
    const count = values.filter((v) => v.passed !== null).length;
    [
      prompt,
      source,
      me.model_labels?.[model] || model,
      count
        ? `${passed} / ${count} (${Math.round((100 * passed) / count)}%)`
        : "Ungraded",
      `${(values.reduce((n, r) => n + (r.elapsed || 0) + (r.judge_elapsed || 0), 0) / values.length).toFixed(1)}s avg`,
    ].forEach((v) => tr.append(el("td", v)));
    summary.append(tr);
  }
  const summaryWrap = el("div", undefined, "table-wrap");
  summaryWrap.append(summary);
  root.append(summaryWrap);
  const filterLabel = el("label", "Answers");
  const filter = el("select");
  filter.setAttribute("aria-label", "Answers");
  for (const [key, label] of [
    ["all", "All answers"],
    ["failed", "Failed or errored"],
    ["passed", "Passed"],
    ["ungraded", "Ungraded"],
  ])
    option(filter, key, label);
  filterLabel.append(filter);
  root.append(filterLabel);
  const answers = el("div");
  root.append(answers);
  function draw() {
    answers.replaceChildren();
    for (const row of rows.filter(
      (r) =>
        filter.value === "all" ||
        (filter.value === "failed" && (r.passed === false || r.error)) ||
        (filter.value === "passed" && r.passed === true) ||
        (filter.value === "ungraded" && r.passed === null),
    )) {
      const detail = el("details", undefined, "evaluation-answer");
      detail.append(
        el(
          "summary",
          `${row.error ? "Error" : row.passed === null ? "Ungraded" : row.passed ? "Pass" : "Fail"} · ${row.scenario || row.input} · ${row.profile} · ${me.model_labels?.[row.model] || row.model || ""}`,
        ),
      );
      for (const [title, value] of [
        ["Question", row.input],
        ["Expected", row.expected],
        ["Answer", row.answer],
        ["Grade", row.reason],
        ["Source group", row.knowledgebase],
        ["Judge model", row.judge_model],
        [
          "Estimated tokens",
          row.estimated_tokens === undefined
            ? ""
            : `${row.estimated_tokens} answer + ${row.judge_tokens || 0} judge`,
        ],
      ]) {
        if (value) detail.append(el("strong", title), el("p", String(value)));
      }
      for (const source of row.sources || []) {
        const d = el("details");
        d.append(
          el("summary", `${source.filename}, page ${source.page}`),
          el("p", source.content),
        );
        detail.append(d);
      }
      if (Object.keys(row.metadata || {}).length)
        detail.append(el("pre", JSON.stringify(row.metadata, null, 2)));
      answers.append(detail);
    }
  }
  filter.onchange = draw;
  draw();
  root.append(
    action("Download results CSV", async () => {
      const cols = [
        "profile",
        "prompt_key",
        "source_profile",
        "knowledgebase",
        "model",
        "model_name",
        "scenario",
        "input",
        "expected",
        "answer",
        "judge_model",
        "passed",
        "score",
        "reason",
        "error",
        "elapsed",
        "judge_elapsed",
        "estimated_tokens",
        "judge_tokens",
        "metadata",
        "sources",
      ];
      const quote = (x) =>
        '"' +
        String(
          typeof x === "object" && x !== null ? JSON.stringify(x) : (x ?? ""),
        ).replaceAll('"', '""') +
        '"';
      const csv = [cols, ...rows.map((r) => cols.map((k) => r[k]))]
        .map((r) => r.map(quote).join(","))
        .join("\r\n");
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
      const a = el("a");
      a.href = url;
      a.download = "asklit-evaluation.csv";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }),
  );
}
bind("publish", async () => {
  await save();
  if (
    !confirm(
      "Publish this assistant and its retrieved source passages for public use?",
    )
  )
    return;
  const r = await api(`/bots/${bot.id}/publish`, "POST", {
    version: bot.version,
  });
  bot.published_id = r.revision_id;
  notice("Published.");
  await refreshResources();
  await loadSharing();
});
bind("copy-bot-id", async () => {
  await navigator.clipboard.writeText(bot.id);
  notice("Bot ID copied.");
});
bind("unpublish", async () => {
  await api(`/bots/${bot.id}/unpublish`, "POST");
  bot.published_id = null;
  notice("Unpublished.");
  await refreshResources();
  await loadSharing();
});
bind("export-bundle", async () => {
  await save();
  await api(`/bots/${bot.id}/export`, "POST");
  notice("Preparing your download…");
  await refreshResources();
});
$("workspace-import").onchange = () =>
  perform(null, async () => {
    const file = $("workspace-import").files[0];
    if (!file) return;
    if (
      dirty &&
      !confirm("Import this workspace and discard unsaved changes?")
    ) {
      $("workspace-import").value = "";
      return;
    }
    let result;
    try {
      result = await api(`/bots/${bot.id}/workspace`, "POST", {
        version: bot.version,
        yaml: await file.text(),
      });
    } finally {
      $("workspace-import").value = "";
    }
    dirty = false;
    await openBot(bot.id);
    notice(
      `${result.notice} Files to reupload: ${result.files_to_reupload.join(", ") || "none"}`,
      false,
      true,
    );
  });
$("workspace-download").onclick = (event) => {
  if (!dirty) return;
  event.preventDefault();
  perform(null, async () => {
    await save();
    window.location.href = $("workspace-download").href;
  });
};
bind("archive", async () => {
  if (!confirm("Archive this assistant and disable its public conversations?"))
    return;
  await api(`/bots/${bot.id}`, "DELETE");
  dirty = false;
  await dashboard();
});
bind("transfer", async () => {
  await api(`/bots/${bot.id}/owner`, "POST", { owner_id: $("owner-id").value });
  dirty = false;
  await dashboard();
  notice("Ownership transferred.");
});
let sharing;
async function loadSharing() {
  const id = bot.id;
  const value = await api(`/bots/${id}/sharing`);
  if (bot?.id !== id) return;
  sharing = value;
  $("sharing-mode").value = value.access_mode;
  $("sharing-slug").value = value.slug;
  $("sharing-prefix").textContent =
    "/" + value.organization.slug + "/";
  $("sharing-gallery-status").textContent = value.access_mode !== "public"
    ? ""
    : !bot.published_id
      ? "Publish this AskLit to list it in the gallery."
      : value.organization.gallery_enabled
        ? "Listed in the gallery."
        : "Not listed: an organization administrator needs to enable the gallery.";
  $("sharing-link").href = value.url;
  $("sharing-link").hidden = !bot.published_id;
  $("password-settings").hidden = value.access_mode !== "password";
  await loadPasswords();
}
async function loadPasswords() {
  const id = bot.id;
  const keys = await api(`/bots/${id}/passwords`);
  if (bot?.id !== id) return;
  $("password-list").replaceChildren();
  for (const key of keys) {
    const row = el("div", undefined, "row");
    row.append(
      el("span", key.label, "grow"),
      el("span", key.enabled ? "Enabled" : "Disabled", "muted"),
      action(key.enabled ? "Disable" : "Enable", async () => {
        await api(`/bots/${id}/passwords/${key.id}`, "PATCH", {
          enabled: !key.enabled,
        });
        await loadPasswords();
      }),
    );
    $("password-list").append(row);
  }
}
$("sharing-mode").onchange = () => {
  $("password-settings").hidden = $("sharing-mode").value !== "password";
};
bind("save-sharing", async () => {
  const input = $("sharing-slug");
  if (!input.value || !input.checkValidity()) {
    input.reportValidity();
    throw new Error(
      "Use lowercase letters, numbers and single dashes for the URL name.",
    );
  }
  await api(`/bots/${bot.id}/sharing`, "PUT", {
    slug: input.value,
    access_mode: $("sharing-mode").value,
  });
  await loadSharing();
  notice("Access and link saved.");
});
bind("copy-link", async () => {
  await navigator.clipboard.writeText(sharing.url);
  notice("Link copied.");
});
bind("add-password", async () => {
  await api(`/bots/${bot.id}/passwords`, "POST", {
    label: $("password-label").value,
    password: $("password-value").value,
  });
  $("password-label").value = "";
  $("password-value").value = "";
  await loadPasswords();
});
bind("save-organization-sharing", async () => {
  const list = (value) =>
    value
      .split(/[\n,]+/)
      .map((x) => x.trim())
      .filter(Boolean);
  const value = await api(`/organizations/${$("org").value}/sharing`, "PUT", {
    slug: $("organization-slug").value,
    gallery_enabled: $("organization-gallery").checked,
    allowed_domains: list($("organization-domains").value),
    microsoft_tenants: list($("organization-tenants").value),
  });
  renderOrgSharing(value);
  notice("Organization settings saved.");
});
function renderOrgSharing(value) {
  $("organization-slug").value = value.slug;
  $("organization-gallery").checked = value.gallery_enabled;
  $("organization-domains").value = value.allowed_domains.join("\n");
  $("organization-tenants").value = value.microsoft_tenants.join("\n");
  $("organization-gallery-link").href = value.gallery_url;
  $("organization-gallery-link").textContent = value.gallery_url;
}
async function organization() {
  show("organization");
  const id = $("org").value;
  const org = me.organizations.find((o) => o.id === id);
  $("org-title").textContent = org.name;
  $("invite-role").querySelector("[value=admin]").disabled =
    org.role !== "owner";
  $("invite-link").textContent = "";
  const [members, audit, organizationSharing] = await Promise.all([
    api(`/organizations/${id}/members`),
    api(`/organizations/${id}/audit`),
    api(`/organizations/${id}/sharing`),
  ]);
  renderOrgSharing(organizationSharing);
  $("members").replaceChildren();
  for (const member of members) {
    const row = el("div", undefined, "row");
    const person = el("div", undefined, "grow");
    person.append(el("strong", member.name), el("div", member.email, "muted"));
    const identity = el("details");
    identity.append(
      el("summary", "Account details"),
      el("p", `Member ID: ${member.id}`),
      el(
        "p",
        member.identities?.some((i) => i.email_verified)
          ? "Verified email"
          : "Unverified contact email",
      ),
      el("p", (member.identities || []).map((i) => i.issuer).join(", ")),
    );
    person.append(identity);
    row.append(person);
    if (org.role !== "owner") row.append(el("span", member.role, "muted"));
    if (org.role === "owner") {
      const select = el("select");
      ["member", "admin", "owner"].forEach((r) => option(select, r, r));
      select.value = member.role;
      row.append(
        select,
        action("Set role", async () => {
          await api(`/organizations/${id}/members/${member.id}`, "PATCH", {
            role: select.value,
          });
          await refreshMe();
          await organization();
        }),
        action("Remove", async () => {
          if (confirm("Remove this person from the organization?")) {
            await api(`/organizations/${id}/members/${member.id}`, "DELETE");
            await organization();
          }
        }),
      );
    }
    $("members").append(row);
  }
  $("audit").replaceChildren(
    ...audit.map((e) =>
      el(
        "p",
        `${new Date(e.created * 1000).toLocaleString()} · ${e.action} · ${e.target}`,
      ),
    ),
  );
}
bind("manage-org", organization);
bind("invite", async () => {
  const r = await api(`/organizations/${$("org").value}/invitations`, "POST", {
    role: $("invite-role").value,
  });
  $("invite-link").textContent = r.url;
});
async function init() {
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.has("invite")) {
    sessionStorage.setItem("asklit-invite", hash.get("invite"));
    historyReplace();
  }
  try {
    await refreshMe();
    const invite = sessionStorage.getItem("asklit-invite");
    if (invite) {
      if (confirm("Join the organization using this invitation?")) {
        await api("/invitations/accept", "POST", { token: invite });
        await refreshMe();
        notice("Organization joined.");
      }
      sessionStorage.removeItem("asklit-invite");
    }
    await dashboard();
  } catch (e) {
    if (e.status === 401) show("login");
    else notice(e.message, true);
  }
}
function historyReplace() {
  window.history.replaceState(null, "", location.pathname);
}
init();

$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing &&
      event.keyCode !== 229 && !event.currentTarget.value.includes("\n")) {
    event.preventDefault();
    const button = $("preview").querySelector(".send-arrow");
    if (!event.repeat && !button.disabled) $("preview").requestSubmit(button);
  }
});

function renderPreviewFiles() {
  $("preview-files").replaceChildren();
  for (const file of previewAttachments) {
    const chip = el("span", file.filename, "attachment");
    chip.append(action("×", () => {
      previewAttachments = previewAttachments.filter(a => a !== file);
      renderPreviewFiles();
    }, "quiet"));
    chip.lastChild.setAttribute("aria-label", "Remove " + file.filename);
    $("preview-files").append(chip);
  }
}
$("preview-attach").onclick = () => $("preview-file").click();
$("preview-file").accept = ".pdf,.docx,.pptx,.xlsx,.odt,.txt,.md,.csv,.html,.htm";
$("preview-file").onchange = async () => {
  const file = $("preview-file").files[0];
  $("preview-file").value = "";
  if (!file || previewUploadBusy || $("preview").querySelector(".send-arrow").disabled) return;
  await perform($("preview-attach"), async () => {
    if (file.size > 5 * 1024 * 1024 || previewAttachments.length >= 3)
      throw new Error("Choose a file under 5 MB, with at most three files.");
    previewUploadBusy = true;
    const id = bot.id;
    const generation = previewGeneration;
    try {
      const body = new FormData();
      body.append("file", file);
      notice("Reading file…");
      const result = await api(`/bots/${id}/preview/attachments`, "POST", body);
      if (bot?.id !== id || generation !== previewGeneration) return;
      if (previewAttachments.reduce((n,a) => n + a.text.length, result.text.length) > 30000)
        throw new Error("These files exceed 30,000 characters. Use shorter excerpts.");
      previewAttachments.push(result);
      renderPreviewFiles();
      notice("");
    } finally { previewUploadBusy = false; }
  });
};
