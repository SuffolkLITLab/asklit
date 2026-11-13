/* Small DOM adapter; the voice lifecycle is shared with the static React client. */
function mountVoicePanel(prefix, options) {
  const node = suffix => document.getElementById(prefix + "-voice-" + suffix);
  let answer = "", answerSources = [], answerId = "", busy = false, active = false, persona = "neutral";
  const config = () => ({ listenMode: node("listen").value, speakMode: node("speak").value, loop: node("loop").checked,
    readReplies: node("read").checked, lang: node("lang").value || navigator.language, persona });
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
      node("finish").hidden = !(active && node("listen").value === "service" && state.stage === "Listening…");
      node("play").disabled = busy || active || !answer;
    },
  });
  node("toggle").onclick = () => active ? voice.stop() : void voice.start(config());
  node("play").onclick = () => void voice.read(answer, config(), answerSources);
  node("finish").onclick = () => voice.finish();
  for (const key of ["listen", "speak", "loop", "read", "lang"]) node(key).onchange = () => {
    voice.stop();
    node("hint").textContent = node("loop").checked
      ? "Speak naturally. Messages send automatically, and the microphone listens again after each reply. Use Stop voice to end."
      : "Dictation fills the message box. Review the text, then send.";
  };
  const poll = setInterval(async () => {
    if (!active || (node("listen").value !== "service" && node("speak").value !== "service")) return;
    try {
      const policy = await options.getPolicy();
      const stt = policy.paid_stt ?? policy.paid, tts = policy.paid_tts ?? policy.paid;
      if ((node("listen").value === "service" && !stt) || (node("speak").value === "service" && !tts)) {
        voice.stop();
        if (!stt) node("listen").value = "browser";
        if (!tts) node("speak").value = "browser";
        options.onError("Paid voice was disabled. Browser voice remains available.");
      }
    } catch (e) { voice.stop(); options.onError(e.message); }
  }, 15000);
  window.addEventListener("pagehide", () => { clearInterval(poll); voice.destroy(); });
  return {
    stop: () => voice.stop(),
    wait: () => voice.wait(),
    reply: (text, sources) => voice.reply(text, sources),
    setBusy(value) {
      busy = value;
      node("toggle").disabled = busy && !active;
      node("play").disabled = busy || active || !answer;
    },
    setAnswer(text, id = "", sources = []) { answer = text; answerId = id; answerSources = sources; node("play").disabled = busy || active || !answer; },
    setPolicy(policy) {
      for (const [key, paid] of [["listen", policy.paid_stt ?? policy.paid], ["speak", policy.paid_tts ?? policy.paid]]) {
        node(key).querySelector('[value="service"]').disabled = !paid;
        if (!paid && node(key).value === "service") { voice.stop(); node(key).value = "browser"; }
      }
      persona = policy.persona || "neutral";
      const panel = document.getElementById(prefix + "-voice-panel");
      if (panel) panel.hidden = policy.enabled === false;
      if (policy.enabled === false) voice.stop();
      if (policy.locked) {  // the instructor chose these; participants only see the result
        for (const [key, mode] of [["listen", policy.listen_mode], ["speak", policy.speak_mode]]) {
          node(key).value = mode;
          node(key).closest("label").hidden = true;
        }
      }
    },
  };
}
