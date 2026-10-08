/*
 * Voice questions for Ask.
 *
 * Tap the mic to record, tap again (or Enter) to stop; Esc cancels. The clip is recorded mono at a low
 * bitrate (~200 KB a minute), capped at 60 seconds, and POSTed as a raw body to /assistant/api/transcribe,
 * which forwards it for transcription. The text lands in the question box to be checked before sending.
 * The microphone is released the moment recording stops.
 *
 *   AskVoice.attach({button, input, endpoint, onText, onError})
 */
(function () {
  "use strict";
  const MAX_SECONDS = 60;
  const TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];

  function supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  }

  function attach(opts) {
    const btn = opts.button, input = opts.input;
    if (!supported()) { btn.hidden = true; return; }
    btn.hidden = false;
    const idle = btn.innerHTML;
    let rec = null, stream = null, chunks = [], timer = null, started = 0, cancelled = false;

    function setState(state, seconds) {
      btn.dataset.state = state;
      btn.disabled = state === "busy";
      if (state === "recording") {
        const s = seconds || 0;
        btn.innerHTML = `<span class="ask-voice-dot"></span>${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
        btn.title = "Stop recording (Esc to cancel)";
      } else if (state === "busy") {
        btn.innerHTML = "…"; btn.title = "Turning your voice into text";
      } else {
        btn.innerHTML = idle; btn.title = "Ask by voice";
      }
    }
    function release() {
      clearInterval(timer); timer = null;
      if (stream) stream.getTracks().forEach(t => t.stop());
      stream = null;
    }
    async function start() {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      } catch (e) {
        opts.onError && opts.onError("Microphone access was blocked. Allow it in the browser to ask by voice.");
        return;
      }
      const mimeType = TYPES.find(t => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
      try {
        rec = new MediaRecorder(stream, mimeType ? { mimeType, audioBitsPerSecond: 24000 } : { audioBitsPerSecond: 24000 });
      } catch (e) {
        release(); opts.onError && opts.onError("This browser can't record audio."); return;
      }
      chunks = []; cancelled = false; started = Date.now();
      rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onstop = finish;
      rec.start(1000);
      setState("recording", 0);
      timer = setInterval(() => {
        const s = Math.floor((Date.now() - started) / 1000);
        if (s >= MAX_SECONDS) stop(); else setState("recording", s);
      }, 250);
    }
    function stop() { if (rec && rec.state !== "inactive") rec.stop(); release(); }
    function cancel() { cancelled = true; stop(); }

    async function finish() {
      const type = (rec && rec.mimeType) || chunks[0]?.type || "audio/webm";
      const blob = new Blob(chunks, { type });
      rec = null; chunks = [];
      if (cancelled || blob.size < 1000) { setState("idle"); return; }
      setState("busy");
      try {
        const r = await fetch(opts.endpoint, { method: "POST", headers: { "Content-Type": type }, body: blob });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || "Couldn't turn the recording into text.");
        const text = String(data.text || "").trim();
        if (!text) throw new Error("Nothing was heard — try again, a little closer to the mic.");
        input.value = text;
        input.dir = "auto";
        input.focus();
        opts.onText && opts.onText(text);
      } catch (e) {
        opts.onError && opts.onError(e.message);
      } finally {
        setState("idle");
      }
    }

    btn.addEventListener("click", () => {
      if (btn.dataset.state === "recording") stop();
      else if (btn.dataset.state !== "busy") start();
    });
    // Capture phase, so Enter stops the recording instead of also sending what's in the box.
    document.addEventListener("keydown", e => {
      if (btn.dataset.state !== "recording") return;
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); }
      else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); stop(); }
    }, true);
    window.addEventListener("pagehide", cancel);
    setState("idle");
  }

  window.AskVoice = { attach, supported };
})();
