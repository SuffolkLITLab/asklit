/* Small DOM adapter; the voice lifecycle is shared with the static React client. */
function mountVoicePanel(prefix, options) {
  const node = suffix => document.getElementById(prefix + "-voice-" + suffix);
  let answer = "", answerId = "", busy = false, active = false;
  const config = () => ({ mode: node("mode").value, loop: node("loop").checked,
    readReplies: node("read").checked, lang: node("lang").value || navigator.language });
  const voice = AskLitVoice.createVoiceController({
    workletUrl: "/static/voice-worklet.js",
    getPolicy: options.getPolicy,
    transcribe: options.transcribe,
    synthesize: text => options.synthesize(text, answerId),
    onTranscript: options.onTranscript,
    onError: options.onError,
    onState: state => {
      active = state.active;
      node("label").textContent = active ? "Stop voice" : "Start voice";
      node("toggle").disabled = busy && !active;
      node("status").textContent = state.stage;
      node("finish").hidden = !(active && node("mode").value === "service" && state.stage === "Listening…");
      node("play").disabled = busy || active || !answer;
    },
  });
  node("toggle").onclick = () => active ? voice.stop() : void voice.start(config());
  node("play").onclick = () => void voice.read(answer, config());
  node("finish").onclick = () => voice.finish();
  for (const key of ["mode", "loop", "read", "lang"]) node(key).onchange = () => voice.stop();
  const poll = setInterval(async () => {
    if (!active || node("mode").value !== "service") return;
    try {
      const policy = await options.getPolicy();
      if (!policy.paid) { voice.stop(); node("mode").value = "browser"; options.onError("Paid voice was disabled. Browser voice remains available."); }
    } catch (e) { voice.stop(); options.onError(e.message); }
  }, 15000);
  window.addEventListener("pagehide", () => { clearInterval(poll); voice.destroy(); });
  return {
    stop: () => voice.stop(),
    wait: () => voice.wait(),
    reply: text => voice.reply(text),
    setBusy(value) {
      busy = value;
      node("toggle").disabled = busy && !active;
      node("play").disabled = busy || active || !answer;
    },
    setAnswer(text, id = "") { answer = text; answerId = id; node("play").disabled = busy || active || !answer; },
    setPolicy(policy) {
      node("mode").querySelector('[value="service"]').disabled = !policy.paid;
      if (!policy.paid && node("mode").value === "service") { voice.stop(); node("mode").value = "browser"; }
    },
  };
}
