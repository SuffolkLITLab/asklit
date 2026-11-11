"use strict";
const $ = (id) => document.getElementById(id);
const botId = $("public-chat").dataset.botId;
let attachments = [];
let metadata,
  accessToken = "",
  session = null,
  retry = null,
  busy = false,
  csrf = "";
async function request(
  path,
  method = "GET",
  body,
  token = "",
  authenticated = false,
  asBlob = false,
) {
  const response = await fetch(path, {
    method,
    credentials: authenticated ? "same-origin" : "omit",
    headers: {
      ...(body && !(body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: "Bearer " + token } : {}),
      ...(authenticated && csrf ? { "X-CSRF-Token": csrf } : {}),
    },
    body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });
  const value = response.ok && asBlob ? null : await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      typeof value.detail === "string"
        ? value.detail
        : "The request could not be completed.",
    );
    error.status = response.status;
    error.code = value.code;
    throw error;
  }
  return asBlob ? response.blob() : value;
}
function showMetadata(data) {
  metadata = data;
  $("chat-title").textContent = data.title;
  document.title = data.title + " · AskLit";
  $("chat-publisher").textContent = "Organization: " + data.organization.name;
  $("chat-identifier").textContent = "AskLit ID: " + data.id;
  $("chat-report").href = "mailto:litlab@suffolk.edu?subject=" + encodeURIComponent("Report AskLit " + data.id);
  $("chat-status").textContent = "";
  $("unlock").hidden = !(data.locked && data.access_mode === "password");
  $("org-unlock").hidden = !(
    data.locked && data.access_mode === "organization"
  );
  $("chat-content").hidden = data.locked;
  if (data.locked) return;
  $("chat-opening").textContent = data.welcome;
  $("chat-footer").textContent = data.footer;
  $("chat-profile").replaceChildren();
  for (const profile of data.profiles) {
    const option = document.createElement("option");
    option.value = profile.key;
    option.textContent = profile.label;
    $("chat-profile").append(option);
  }
  $("chat-profile-label").hidden = data.profiles.length < 2;
  $("chat-attach").hidden = !data.uploads?.enabled;
  $("chat-upload-note").hidden = !data.uploads?.enabled;
  $("chat-file").accept = data.uploads?.extensions.join(",") || "";
  chatVoice.setPolicy(data.voice || { browser: true, paid: false });
  renderStarters();
}
function renderStarters() {
  $("chat-starters").replaceChildren();
  const profile = metadata.profiles.find(
    (p) => p.key === $("chat-profile").value,
  );
  for (const text of profile?.conversation_starters || []) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "secondary";
    b.textContent = text;
    b.onclick = () => {
      $("chat-question").value = text;
      $("chat-question").focus();
    };
    $("chat-starters").append(b);
  }
}
function report(error) {
  $("chat-error").textContent = error.message;
}
async function unlock(password, organization = false) {
  const result = await request(
    `/api/public/bots/${botId}/access`,
    "POST",
    { password },
    "",
    organization,
  );
  accessToken = result.token;
  $("chat-password").value = "";
  $("chat-error").textContent = "";
  showMetadata(result.metadata);
  const origin = $("public-chat").dataset.opener;
  if (organization && origin && window.opener) {
    window.opener.postMessage(
      { type: "asklit-access", botId, token: accessToken },
      origin,
    );
    window.close();
  }
}
$("unlock").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await unlock($("chat-password").value);
  } catch (e) {
    report(e);
  } finally {
    button.disabled = false;
  }
};
$("org-continue").onclick = async () => {
  $("org-continue").disabled = true;
  try {
    await unlock("", true);
  } catch (e) {
    report(e);
  } finally {
    $("org-continue").disabled = false;
  }
};
function append(role, text, sources = []) {
  const div = document.createElement("div");
  div.className = "message " + role;
  div.textContent = text;
  for (const source of sources) {
    const d = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `${source.filename}, page ${source.page}`;
    const p = document.createElement("p");
    p.textContent = source.content;
    d.append(summary, p);
    div.append(d);
  }
  $("chat-messages").append(div);
  div.scrollIntoView({ block: "nearest" });
  return div;
}
function setBusy(value) {
  busy = value;
  chatVoice.setBusy(value);
  for (const id of ["chat-submit", "chat-clear", "chat-attach", "chat-file"]) $(id).disabled = value;
  $("chat-profile").disabled = value || !!session;
  renderFiles();
}
$("chat-send").onsubmit = async (event) => {
  event.preventDefault();
  if (busy) return;
  const question = $("chat-question").value.trim();
  if (!question) return;
  chatVoice.wait();
  setBusy(true);
  $("chat-error").textContent = "";
  let pending;
  try {
    if (!session)
      session = await request(
        `/api/public/bots/${botId}/conversations`,
        "POST",
        { profile: $("chat-profile").value, access_token: accessToken },
      );
    if (metadata.uploads?.enabled) {
      attachments = await request(`/api/public/conversations/${session.id}/attachments`, "GET", undefined, session.token);
      renderFiles();
    }
    retry =
      retry?.message === question
        ? retry
        : { message: question, request_id: crypto.randomUUID() };
    pending = append("assistant", "Thinking…");
    const result = await request(
      `/api/public/conversations/${session.id}/messages`,
      "POST",
      retry,
      session.token,
    );
    pending.remove();
    pending = null;
    append("user", question);
    append("assistant", result.answer, result.sources);
    chatVoice.setAnswer(result.answer, retry.request_id);
    retry = null;
    $("chat-question").value = "";
    await chatVoice.reply(result.answer);
  } catch (e) {
    chatVoice.stop();
    if (e.status && !["turn_pending", "conversation_busy"].includes(e.code))
      retry = null;
    if (e.status === 403 || e.status === 404) {
      session = null;
      chatVoice.setAnswer("", "");
      attachments = [];
      renderFiles();
      accessToken = "";
      try {
        showMetadata(await request(`/api/public/bots/${botId}`));
      } catch {}
    }
    report(e);
  } finally {
    pending?.remove();
    setBusy(false);
  }
};
$("chat-clear").onclick = async () => {
  if (busy) return;
  chatVoice.stop();
  try {
    if (session)
      await request(
        `/api/public/conversations/${session.id}`,
        "DELETE",
        undefined,
        session.token,
      );
    session = null;
    retry = null;
    attachments = [];
    renderFiles();
    $("chat-messages").replaceChildren();
    chatVoice.setAnswer("");
    $("chat-error").textContent = "";
    $("chat-profile").disabled = false;
  } catch (e) {
    if (e.status === 404) {
      session = null;
      retry = null;
      attachments = [];
      renderFiles();
      $("chat-messages").replaceChildren();
    chatVoice.setAnswer("");
      $("chat-profile").disabled = false;
    } else report(e);
  }
};
$("chat-profile").onchange = () => { chatVoice.stop(); renderStarters(); };
async function voiceSession() {
  if (!session) {
    const previous = busy; setBusy(true);
    try { session = await request(`/api/public/bots/${botId}/conversations`, "POST",
      { profile: $("chat-profile").value, access_token: accessToken }); }
    finally { setBusy(previous); }
  }
  return session;
}
async function paidVoiceRequest(path, body, token, asBlob = false) {
  const previous = busy;
  setBusy(true);
  try { return await request(path, "POST", body, token, false, asBlob); }
  finally { setBusy(previous); }
}
const chatVoice = mountVoicePanel("chat", {
  getPolicy: async () => {
    if (session) return request(`/api/public/conversations/${session.id}/voice`, "GET", undefined, session.token);
    const data = await request(`/api/public/bots/${botId}`, "GET", undefined, accessToken);
    if (data.locked) throw new Error("Unlock this AskLit before using voice.");
    return data.voice || { browser: true, paid: false };
  },
  onTranscript: async (text, auto) => {
    $("chat-question").value = [$("chat-question").value.trim(), text].filter(Boolean).join(" ");
    if (auto) await $("chat-send").onsubmit({ preventDefault() {} });
    else $("chat-question").focus();
  },
  transcribe: async audio => {
    const current = await voiceSession();
    const body = new FormData(); body.append("file", audio, "recording.wav");
    const result = await paidVoiceRequest(`/api/public/conversations/${current.id}/transcribe`, body, current.token);
    return result.text;
  },
  synthesize: async (text, requestId) => {
    const current = await voiceSession();
    return paidVoiceRequest(`/api/public/conversations/${current.id}/speech`, { request_id: requestId }, current.token, true);
  },
  onError: text => report(new Error(text)),
});
(async () => {
  try {
    showMetadata(await request(`/api/public/bots/${botId}`));
    if (metadata.locked && metadata.access_mode === "organization") {
      try {
        const me = await request("/api/me", "GET", undefined, "", true);
        csrf = me.csrf;
        $("org-continue").hidden = false;
        const origin = $("public-chat").dataset.opener;
        $("org-continue").textContent = origin
          ? `Continue to ${new URL(origin).host}`
          : "Open AskLit";
      } catch {}
    }
  } catch (e) {
    $("chat-status").textContent = "";
    report(e);
  }
})();

$("chat-question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing &&
      event.keyCode !== 229 && !event.currentTarget.value.includes("\n")) {
    event.preventDefault();
    const button = $("chat-send").querySelector(".send-arrow");
    if (!event.repeat && !button.disabled) $("chat-send").requestSubmit(button);
  }
});

function renderFiles() {
  $("chat-files").replaceChildren();
  for (const file of attachments) {
    const chip = document.createElement("span");
    chip.className = "attachment";
    chip.append(document.createTextNode(file.filename + " "));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "quiet";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Remove " + file.filename);
    remove.disabled = busy;
    remove.onclick = async () => {
      if (busy || !session) return;
      setBusy(true);
      try {
        await request(`/api/public/conversations/${session.id}/attachments/${file.id}`, "DELETE", undefined, session.token);
        attachments = attachments.filter(a => a.id !== file.id);
        retry = null;
      } catch (e) { report(e); }
      finally { setBusy(false); }
    };
    chip.append(remove);
    $("chat-files").append(chip);
  }
}
$("chat-attach").onclick = () => $("chat-file").click();
$("chat-file").onchange = async () => {
  const file = $("chat-file").files[0];
  $("chat-file").value = "";
  if (!file || busy) return;
  chatVoice.stop();
  if (file.size > metadata.uploads.max_bytes || attachments.length >= metadata.uploads.max_files) {
    report(new Error("Choose a file under 5 MB, with at most three files per conversation."));
    return;
  }
  setBusy(true);
  $("chat-error").textContent = "";
  $("chat-status").textContent = "Reading file…";
  try {
    if (!session) session = await request(`/api/public/bots/${botId}/conversations`, "POST",
      {profile: $("chat-profile").value, access_token: accessToken});
    const body = new FormData();
    body.append("file", file);
    try {
      await request(`/api/public/conversations/${session.id}/attachments`, "POST", body, session.token);
    } finally {
      // Reconcile a lost response without uploading a duplicate.
      attachments = await request(`/api/public/conversations/${session.id}/attachments`, "GET", undefined, session.token);
    }
    retry = null;
  } catch (e) {
    if ([403, 404].includes(e.status)) {
      session = null; accessToken = ""; attachments = [];
      try { showMetadata(await request(`/api/public/bots/${botId}`)); } catch {}
    }
    report(e);
  } finally {
    $("chat-status").textContent = "";
    setBusy(false);
  }
};
