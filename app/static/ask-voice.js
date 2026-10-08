/*
 * Voice questions for Ask. Two ways to turn speech into text, picked automatically:
 *
 *  • Browser (default) — the browser's own speech recognition (Chrome, Edge, Safari). The words appear in
 *    the question box as they're spoken. Costs nothing and needs nothing on the server; in Chrome/Edge the
 *    audio is processed by Google's/Microsoft's speech service. Needs a language, so a small ع / EN switch
 *    sits next to the mic and is remembered.
 *  • Server — when the server has a transcription model (status.can_transcribe): a short clip is recorded
 *    mono at a low bitrate (~200 KB a minute, 60 s max) and POSTed raw to /assistant/api/transcribe.
 *
 * Either way: tap the mic to start, tap again (or Enter) to stop, Esc cancels; the text lands in the box to
 * be checked before sending, and the microphone is released as soon as it stops.
 *
 *   AskVoice.attach({button, input, endpoint, server, onError})
 */
(function () {
  "use strict";
  const MAX_SECONDS = 60;
  const TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
  const LANGS = { ar: { code: "ar-EG", label: "ع" }, en: { code: "en-US", label: "EN" } };
  const LANG_KEY = "azed-ask-voice-lang";
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  const canRecord = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  const canRecognise = () => !!Recognition;

  function attach(opts) {
    const btn = opts.button, input = opts.input;
    const server = !!opts.server && canRecord();
    if (!server && !canRecognise()) { btn.hidden = true; return; }
    if (btn.dataset.state) return;            // already attached
    btn.hidden = false;
    const idle = btn.innerHTML;
    const fail = msg => opts.onError && opts.onError(msg);
    let timer = null, started = 0, active = null;   // active: {stop(), cancel()}

    function setState(state, seconds) {
      btn.dataset.state = state;
      btn.disabled = state === "busy";
      if (state === "recording") {
        const s = seconds || 0;
        btn.innerHTML = `<span class="ask-voice-dot"></span>${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
        btn.title = "Stop (Enter) · cancel (Esc)";
      } else if (state === "busy") {
        btn.innerHTML = "…"; btn.title = "Turning your voice into text";
      } else {
        btn.innerHTML = idle; btn.title = "Ask by voice";
      }
    }
    function startClock(onLimit) {
      started = Date.now();
      setState("recording", 0);
      timer = setInterval(() => {
        const s = Math.floor((Date.now() - started) / 1000);
        if (s >= MAX_SECONDS) onLimit(); else setState("recording", s);
      }, 250);
    }
    function stopClock() { clearInterval(timer); timer = null; }
    function done(text) {
      stopClock(); active = null; setState("idle");
      if (text) { input.value = text; input.dir = "auto"; }
      input.focus();
    }

    // ── Language switch (browser mode only) ──
    let lang = "ar";
    try { lang = localStorage.getItem(LANG_KEY) === "en" ? "en" : "ar"; } catch (e) {}
    let langBtn = null;
    if (!server) {
      langBtn = document.createElement("button");
      langBtn.type = "button";
      langBtn.className = "ask-voice-lang";
      const paint = () => {
        langBtn.textContent = LANGS[lang].label;
        langBtn.title = lang === "ar" ? "Speaking Arabic — tap for English" : "Speaking English — tap for Arabic";
        langBtn.setAttribute("aria-label", langBtn.title);
      };
      langBtn.onclick = () => {
        if (active) return;
        lang = lang === "ar" ? "en" : "ar";
        try { localStorage.setItem(LANG_KEY, lang); } catch (e) {}
        paint();
      };
      paint();
      btn.insertAdjacentElement("beforebegin", langBtn);
    }

    // ── Browser speech recognition ──
    function startBrowser() {
      const rec = new Recognition();
      rec.lang = LANGS[lang].code;
      rec.continuous = true;
      rec.interimResults = true;
      const before = input.value;
      let finalText = "", cancelled = false, failed = false;
      rec.onresult = e => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          if (r.isFinal) finalText += r[0].transcript + " ";
          else interim += r[0].transcript;
        }
        input.value = (finalText + interim).trim();
        input.dir = "auto";
      };
      rec.onerror = e => {
        failed = true;
        const why = {
          "not-allowed": "Microphone access was blocked. Allow it in the browser to ask by voice.",
          "service-not-allowed": "Speech recognition isn't allowed in this browser.",
          "no-speech": "Nothing was heard — try again, a little closer to the mic.",
          "audio-capture": "No microphone was found.",
          "network": "The browser's speech service couldn't be reached. Check the connection, or type it.",
          "language-not-supported": "This browser can't recognise that language — switch ع / EN.",
        }[e.error];
        if (e.error !== "aborted") fail(why || "Voice input stopped. Try again, or type it.");
      };
      rec.onend = () => {
        const text = finalText.trim() || (failed || cancelled ? "" : input.value.trim());
        if (cancelled || (failed && !text)) { input.value = before; done(""); return; }
        done(text);
      };
      active = { stop: () => rec.stop(), cancel: () => { cancelled = true; rec.abort(); } };
      try { rec.start(); } catch (e) { active = null; fail("Voice input couldn't start. Try again."); return; }
      input.value = "";
      startClock(() => rec.stop());
    }

    // ── Server transcription ──
    async function startServer() {
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      } catch (e) {
        fail("Microphone access was blocked. Allow it in the browser to ask by voice."); return;
      }
      const release = () => stream.getTracks().forEach(t => t.stop());
      const mimeType = TYPES.find(t => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
      let rec;
      try {
        rec = new MediaRecorder(stream, mimeType ? { mimeType, audioBitsPerSecond: 24000 } : { audioBitsPerSecond: 24000 });
      } catch (e) { release(); fail("This browser can't record audio."); return; }
      const chunks = [];
      let cancelled = false;
      rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onstop = async () => {
        release(); stopClock();
        const type = rec.mimeType || (chunks[0] && chunks[0].type) || "audio/webm";
        const blob = new Blob(chunks, { type });
        chunks.length = 0;
        if (cancelled || blob.size < 1000) { done(""); return; }
        setState("busy");
        try {
          const r = await fetch(opts.endpoint, { method: "POST", headers: { "Content-Type": type }, body: blob });
          const data = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(data.detail || "Couldn't turn the recording into text.");
          const text = String(data.text || "").trim();
          if (!text) throw new Error("Nothing was heard — try again, a little closer to the mic.");
          done(text);
        } catch (e) { fail(e.message); done(""); }
      };
      active = { stop: () => rec.state !== "inactive" && rec.stop(),
                 cancel: () => { cancelled = true; if (rec.state !== "inactive") rec.stop(); } };
      rec.start(1000);
      startClock(() => active && active.stop());
    }

    btn.addEventListener("click", () => {
      if (btn.dataset.state === "recording") { active && active.stop(); return; }
      if (btn.dataset.state === "busy") return;
      server ? startServer() : startBrowser();
    });
    // Capture phase, so Enter stops the recording instead of also sending what's in the box.
    document.addEventListener("keydown", e => {
      if (btn.dataset.state !== "recording" || !active) return;
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); active.cancel(); }
      else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); active.stop(); }
    }, true);
    window.addEventListener("pagehide", () => active && active.cancel());
    setState("idle");
  }

  window.AskVoice = { attach, canRecord, canRecognise };
})();
