"use strict";
const $ = (id) => document.getElementById(id);
const botId = $("public-chat").dataset.botId;
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
) {
  const response = await fetch(path, {
    method,
    credentials: authenticated ? "same-origin" : "omit",
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: "Bearer " + token } : {}),
      ...(authenticated && csrf ? { "X-CSRF-Token": csrf } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const value = await response.json().catch(() => ({}));
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
  return value;
}
function showMetadata(data) {
  metadata = data;
  $("chat-title").textContent = data.title;
  document.title = data.title + " · AskLit";
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
  for (const id of ["chat-submit", "chat-clear"]) $(id).disabled = value;
  $("chat-profile").disabled = value || !!session;
}
$("chat-send").onsubmit = async (event) => {
  event.preventDefault();
  if (busy) return;
  const question = $("chat-question").value.trim();
  if (!question) return;
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
    retry = null;
    $("chat-question").value = "";
  } catch (e) {
    if (e.status && !["turn_pending", "conversation_busy"].includes(e.code))
      retry = null;
    if (e.status === 403 || e.status === 404) {
      session = null;
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
    $("chat-messages").replaceChildren();
    $("chat-error").textContent = "";
    $("chat-profile").disabled = false;
  } catch (e) {
    if (e.status === 404) {
      session = null;
      retry = null;
      $("chat-messages").replaceChildren();
      $("chat-profile").disabled = false;
    } else report(e);
  }
};
$("chat-profile").onchange = renderStarters;
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
