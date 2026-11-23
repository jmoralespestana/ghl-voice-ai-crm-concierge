const SILENT_WAV_URI =
  "data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA";

const sofiaAudio = new Audio();
sofiaAudio.preload = "auto";

const state = {
  lots: [],
  selectedLot: "LOT-AV-01",
  termMonths: 60,
  docType: "ITIN",
  downUsd: 3500,
  snapshot: null,
  voiceWs: null,
  voiceStartedAt: 0,
  mediaRecorder: null,
  micStream: null,
  vadInterval: null,
  audioCtx: null,
  audioChunks: [],
  audioUnlocked: false,
  autoSpeakChat: true,
  handsFreeMode: true,
};

function fmtUsd(n, decimals = 0) {
  return "$" + Number(n || 0).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function unlockBrowserAudio() {
  if (state.audioUnlocked) return;
  try {
    sofiaAudio.src = SILENT_WAV_URI;
    const p = sofiaAudio.play();
    if (p && typeof p.then === "function") {
      p.then(() => {
        sofiaAudio.pause();
        sofiaAudio.currentTime = 0;
        state.audioUnlocked = true;
      }).catch(() => {});
    } else {
      state.audioUnlocked = true;
    }
  } catch (_) {}
}

function stopSofiaAudio() {
  try {
    sofiaAudio.onended = null;
    sofiaAudio.onerror = null;
    sofiaAudio.pause();
    sofiaAudio.currentTime = 0;
  } catch (_) {}
  if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }
}

async function playSofiaMp3(mp3Base64, fallbackText = "", onEnded = null) {
  stopSofiaAudio();
  let b64 = mp3Base64 || "";
  if (!b64 && fallbackText) {
    try {
      const r = await fetch("/api/v1/voice/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: fallbackText }),
      });
      const d = await r.json();
      b64 = d.audio_mp3_base64 || "";
    } catch (_) {}
  }

  if (b64) {
    sofiaAudio.src = "data:audio/mp3;base64," + b64;
    sofiaAudio.onended = () => {
      sofiaAudio.onended = null;
      if (typeof onEnded === "function") onEnded();
    };
    sofiaAudio.onerror = () => {
      sofiaAudio.onerror = null;
      if (typeof onEnded === "function") onEnded();
    };
    try {
      await sofiaAudio.play();
      state.audioUnlocked = true;
      return;
    } catch (err) {
      console.warn("Audio play blocked or failed:", err);
    }
  }

  if ("speechSynthesis" in window && fallbackText) {
    const u = new SpeechSynthesisUtterance(fallbackText);
    u.lang = "es-US";
    u.onend = () => {
      if (typeof onEnded === "function") onEnded();
    };
    window.speechSynthesis.speak(u);
    if ((window.speechSynthesis.getVoices() || []).length === 0 && typeof onEnded === "function") {
      setTimeout(onEnded, 300);
    }
  } else if (typeof onEnded === "function") {
    setTimeout(onEnded, 200);
  }
}

function toggleChatAudio() {
  unlockBrowserAudio();
  state.autoSpeakChat = !state.autoSpeakChat;
  if (!state.autoSpeakChat) stopSofiaAudio();
  const btn = document.getElementById("btn-toggle-chat-audio");
  if (btn) {
    btn.textContent = state.autoSpeakChat ? "🔊 Voz Activa: ON" : "🔇 Voz Activa: OFF";
    btn.className = state.autoSpeakChat ? "btn-xs approve" : "btn-xs";
  }
}

function toggleHandsFree() {
  state.handsFreeMode = !state.handsFreeMode;
  const btn = document.getElementById("btn-handsfree");
  if (btn) {
    btn.textContent = state.handsFreeMode ? "🔁 Manos Libres: ON" : "🔁 Manos Libres: OFF";
    btn.className = state.handsFreeMode ? "btn-xs approve" : "btn-xs";
  }
  if (!state.handsFreeMode && state.mediaRecorder && state.mediaRecorder.state === "recording") {
    stopMicCaptureOnly(true);
  } else if (state.handsFreeMode && state.voiceWs && sofiaAudio.paused) {
    startSmartVoiceCapture();
  }
}

async function speakMessageById(msgId) {
  unlockBrowserAudio();
  if (!state.snapshot) return;
  const msg = (state.snapshot.messages || []).find((m) => m.id === msgId);
  if (msg && msg.body) {
    await playSofiaMp3("", msg.body);
  }
}

function appendVoiceDrawerTurn(speaker, text) {
  const log = document.getElementById("voice-live-log");
  if (!log || !text) return;
  const div = document.createElement("div");
  div.style.padding = "0.28rem 0.45rem";
  div.style.borderRadius = "6px";
  div.style.background =
    speaker === "TU" ? "rgba(59,130,246,0.16)" : "rgba(34,197,94,0.14)";
  div.innerHTML = `<strong>${speaker === "TU" ? "🗣️ Tú:" : "🤖 Sofía:"}</strong> ${text}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function switchView(target) {
  const isBuyer = target === "buyer";
  document.getElementById("view-buyer").classList.toggle("hidden", !isBuyer);
  document.getElementById("view-director").classList.toggle("hidden", isBuyer);
  document.getElementById("tab-buyer").classList.toggle("active", isBuyer);
  document.getElementById("tab-director").classList.toggle("active", !isBuyer);
  refreshSnapshot();
}

async function loadLots() {
  const res = await fetch("/api/v1/lots");
  const data = await res.json();
  state.lots = data.lots || [];
  renderLots();
  await updateQuote();
}

function renderLots() {
  const wrap = document.getElementById("lots-container");
  wrap.innerHTML = state.lots
    .map((lot) => {
      const sel = lot.code === state.selectedLot ? "selected" : "";
      const resv = lot.status === "RESERVED" ? "reserved" : "";
      return `
      <div class="lot-card ${sel} ${resv}" onclick="selectLot('${lot.code}')">
        <div class="lot-header">
          <span>${lot.code} · ${lot.city}</span>
          <span class="status-dot status-${lot.status}">${lot.status}</span>
        </div>
        <div style="font-weight:700;font-size:0.95rem;">${lot.title}</div>
        <div class="lot-price">${fmtUsd(lot.cash_price_usd)} USD</div>
        <div class="lot-meta">${lot.acres} · Enganche mín: <strong>${fmtUsd(lot.min_down_payment_usd)}</strong></div>
        <div class="lot-meta" style="margin-top:0.25rem;">${lot.utilities_summary}</div>
      </div>`;
    })
    .join("");
}

async function selectLot(code) {
  state.selectedLot = code;
  const lot = state.lots.find((l) => l.code === code);
  if (lot) {
    state.downUsd = lot.min_down_payment_usd;
    document.getElementById("down-slider").value = state.downUsd;
    document.getElementById("down-label").textContent = `${fmtUsd(state.downUsd)} USD`;
    document.getElementById("calc-lot-badge").textContent = `${lot.code} (${lot.city})`;
  }
  renderLots();
  await updateQuote();
}

async function updateQuote() {
  const res = await fetch("/api/v1/financing/quote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      lot_code: state.selectedLot,
      down_payment_usd: state.downUsd,
      term_months: state.termMonths,
      doc_type: state.docType,
    }),
  });
  const q = await res.json();
  document.getElementById("quote-monthly").textContent =
    state.docType === "CASH" ? "$0.00 USD (Contado)" : `${fmtUsd(q.monthly_payment_usd, 2)} USD/mes`;
  document.getElementById("quote-cash").textContent = `${fmtUsd(q.cash_price_usd)} USD`;
  document.getElementById("quote-principal").textContent = `${fmtUsd(q.financed_principal_usd)} USD`;

  const warn = document.getElementById("quote-hitl-warning");
  if (!q.available_for_sale) {
    warn.classList.remove("hidden");
    warn.textContent = `🛑 Lote ${q.lot_code} marcado como APARTADO en inventario vivo. Sofía IA bloqueará cualquier intento de venta sobre esta parcela.`;
  } else if (q.requires_hitl_exception) {
    warn.classList.remove("hidden");
    warn.textContent = `⚠️ El enganche seleccionado (${fmtUsd(q.down_payment_usd)}) es menor al mínimo oficial (${fmtUsd(q.min_down_payment_usd)}). Sofía IA escalará a aprobación HITL del Director Comercial.`;
  } else {
    warn.classList.add("hidden");
  }
}

async function sendCalculatorToChat() {
  unlockBrowserAudio();
  const msg = `Me interesa ${state.selectedLot} calificando con ${state.docType}, dando ${fmtUsd(state.downUsd)} de enganche a ${state.termMonths} meses.`;
  await quickChat(msg);
}

async function quickChat(message) {
  unlockBrowserAudio();
  const name = document.getElementById("buyer-name").value || "Prospecto Demo";
  const phone = document.getElementById("buyer-phone").value || "+17605550199";
  const res = await fetch("/api/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      full_name: name,
      phone,
      message,
      channel: "WEB_CHAT",
      selected_lot_code: state.selectedLot,
      term_months: state.termMonths,
      doc_type: state.docType,
      include_audio: state.autoSpeakChat,
    }),
  });
  const data = await res.json();
  await refreshSnapshot();
  if (state.autoSpeakChat && !data.bot_silenced && (data.audio_mp3_base64 || data.reply)) {
    await playSofiaMp3(data.audio_mp3_base64, data.reply || "");
  }
}

async function submitChatForm(e) {
  e.preventDefault();
  unlockBrowserAudio();
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  await quickChat(text);
}

function stopMicCaptureOnly(discard = false) {
  if (state.vadInterval) {
    clearInterval(state.vadInterval);
    state.vadInterval = null;
  }
  const bar = document.getElementById("mic-level-bar");
  if (bar) bar.style.width = "0%";
  if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
    state.mediaRecorder._discardOnStop = discard;
    state.mediaRecorder.stop();
  } else if (state.micStream) {
    state.micStream.getTracks().forEach((t) => t.stop());
    state.micStream = null;
  }
}

async function toggleVoiceCall() {
  unlockBrowserAudio();
  const btn = document.getElementById("voice-call-btn");
  const drawer = document.getElementById("voice-drawer");
  const provider = document.getElementById("voice-strategy-select").value;
  const name = document.getElementById("buyer-name").value || "Prospecto Demo";
  const phone = document.getElementById("buyer-phone").value || "+17605550199";

  if (state.voiceWs) {
    stopSofiaAudio();
    stopMicCaptureOnly(true);
    const dur = Math.max(8, Math.round((Date.now() - state.voiceStartedAt) / 1000));
    try {
      state.voiceWs.send(JSON.stringify({ type: "end_call", duration_seconds: dur }));
      state.voiceWs.close();
    } catch (_) {}
    state.voiceWs = null;
    btn.classList.remove("active-call");
    btn.textContent = "🎙️ Hablar por Voz con Sofía Ahora (Sin Exponer Teléfono · Aviso CA § 632)";
    drawer.classList.add("hidden");
    setTimeout(refreshSnapshot, 350);
    return;
  }

  const logEl = document.getElementById("voice-live-log");
  if (logEl) logEl.innerHTML = "";
  document.getElementById("voice-status-text").textContent = "⏳ Conectando línea web segura con Sofía…";
  drawer.classList.remove("hidden");

  const sessRes = await fetch("/api/v1/voice/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, buyer_name: name, buyer_phone: phone }),
  });
  const sess = await sessRes.json();

  if (provider === "retell" && !sess.retell_access_token) {
    document.getElementById("voice-ca-notice").textContent =
      `[Retell Adapter Listo · 0 teléfono expuesto] ${sess.ca_632_opening_es}`;
    document.getElementById("voice-status-text").textContent = "🔊 Sofía reproduciendo aviso CA § 632…";
    await playSofiaMp3("", sess.ca_632_opening_es, () => {
      document.getElementById("voice-status-text").textContent = "🟡 Modo Retell Preview (usa Gemini Live WS para conversar en vivo)";
    });
    return;
  }

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const qs = new URLSearchParams({
    buyer_name: name,
    buyer_phone: phone,
    lot_code: state.selectedLot,
  });
  const ws = new WebSocket(`${proto}//${location.host}/api/v1/voice/gemini-live/ws?${qs}`);
  state.voiceWs = ws;
  state.voiceStartedAt = Date.now();
  btn.classList.add("active-call");
  btn.textContent = "⏹️ Colgar Llamada y Sincronizar Grabación/Resumen en GoHighLevel";

  ws.onmessage = async (ev) => {
    const data = JSON.parse(ev.data);
    if (data.type === "ca_632_greeting") {
      document.getElementById("voice-ca-notice").textContent = "⚖️ " + data.spoken_text_es;
      document.getElementById("voice-status-text").textContent = "🔊 Sofía hablando (Aviso Legal CA § 632)…";
      appendVoiceDrawerTurn("SOFIA", data.spoken_text_es);
      await playSofiaMp3(data.audio_mp3_base64, data.spoken_text_es, () => {
        if (!state.voiceWs) return;
        if (state.handsFreeMode) {
          startSmartVoiceCapture();
        } else {
          document.getElementById("voice-status-text").textContent = "🟢 Tu turno: pulsa '🎤 Hablar' o escribe abajo";
        }
      });
    } else if (data.type === "assistant_utterance") {
      if (data.user_transcript) {
        appendVoiceDrawerTurn("TU", data.user_transcript);
      }
      if (data.bot_silenced) {
        document.getElementById("voice-status-text").textContent = "✋ Asesor humano tomó la llamada (IA en silencio)";
        refreshSnapshot();
        return;
      }
      const toolsUsed = (data.tool_calls || []).map((t) => t.name).join(", ");
      document.getElementById("voice-status-text").textContent = toolsUsed
        ? `🔊 Sofía hablando (Tools invocadas: ${toolsUsed})…`
        : "🔊 Sofía hablando…";
      if (data.spoken_text_es) {
        appendVoiceDrawerTurn("SOFIA", data.spoken_text_es);
      }
      refreshSnapshot();
      await playSofiaMp3(data.audio_mp3_base64, data.spoken_text_es || "", () => {
        if (!state.voiceWs) return;
        if (state.handsFreeMode) {
          startSmartVoiceCapture();
        } else {
          document.getElementById("voice-status-text").textContent = "🟢 Tu turno: pulsa '🎤 Hablar' o escribe abajo";
        }
      });
    } else if (data.type === "call_synced") {
      refreshSnapshot();
    }
  };
}

function sendSpokenTurn() {
  unlockBrowserAudio();
  const input = document.getElementById("voice-utterance-input");
  const txt = input.value.trim();
  if (!txt || !state.voiceWs) return;
  stopSofiaAudio();
  stopMicCaptureOnly(true);
  input.value = "";
  document.getElementById("voice-status-text").textContent = "🧠 Sofía pensando y consultando catálogo…";
  state.voiceWs.send(JSON.stringify({
    type: "user_utterance",
    text: txt,
    lot_code: state.selectedLot,
    use_vertex: true,
  }));
}

async function toggleAudioRecording() {
  unlockBrowserAudio();
  if (!state.voiceWs) {
    alert("Primero inicia la llamada con el botón verde de arriba.");
    return;
  }
  if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
    stopMicCaptureOnly(false);
    return;
  }
  stopSofiaAudio();
  await startSmartVoiceCapture();
}

async function startSmartVoiceCapture() {
  const recBtn = document.getElementById("btn-record-audio");
  if (!state.voiceWs) return;
  if (state.mediaRecorder && state.mediaRecorder.state === "recording") return;

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (!state.voiceWs) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    state.micStream = stream;
    state.audioChunks = [];
    const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : "audio/webm";
    const mr = new MediaRecorder(stream, { mimeType });
    mr._discardOnStop = false;
    state.mediaRecorder = mr;

    mr.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) state.audioChunks.push(e.data);
    };

    mr.onstop = async () => {
      if (state.vadInterval) {
        clearInterval(state.vadInterval);
        state.vadInterval = null;
      }
      const bar = document.getElementById("mic-level-bar");
      if (bar) bar.style.width = "0%";
      stream.getTracks().forEach((t) => t.stop());
      state.micStream = null;
      if (recBtn) recBtn.textContent = "🎤 Hablar";

      if (mr._discardOnStop || !state.voiceWs || state.audioChunks.length === 0) {
        return;
      }
      const blob = new Blob(state.audioChunks, { type: "audio/webm" });
      if (blob.size < 800) return;

      const buf = await blob.arrayBuffer();
      let binary = "";
      const bytes = new Uint8Array(buf);
      for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
      const b64 = btoa(binary);
      document.getElementById("voice-status-text").textContent = "🧠 Escuchando tu consulta y respondiendo…";
      state.voiceWs.send(JSON.stringify({
        type: "user_audio",
        audio_base64: b64,
        mime_type: "audio/webm",
        lot_code: state.selectedLot,
      }));
    };

    // Real-time microphone level & silence auto-send (Hands-Free VAD)
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (AudioCtx) {
      if (!state.audioCtx) state.audioCtx = new AudioCtx();
      if (state.audioCtx.state === "suspended") await state.audioCtx.resume();
      const source = state.audioCtx.createMediaStreamSource(stream);
      const analyser = state.audioCtx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const pcm = new Float32Array(analyser.fftSize);
      let heardSpeech = false;
      let speechMs = 0;
      let silenceAfterSpeechMs = 0;
      let totalElapsedMs = 0;
      const bar = document.getElementById("mic-level-bar");

      state.vadInterval = setInterval(() => {
        if (!state.mediaRecorder || state.mediaRecorder.state !== "recording") return;
        analyser.getFloatTimeDomainData(pcm);
        let sumSq = 0;
        for (let i = 0; i < pcm.length; i++) sumSq += pcm[i] * pcm[i];
        const rms = Math.sqrt(sumSq / pcm.length);
        const pct = Math.min(100, Math.round(rms * 550));
        if (bar) bar.style.width = pct + "%";

        totalElapsedMs += 80;
        if (rms > 0.018) {
          speechMs += 80;
          if (speechMs >= 240) heardSpeech = true;
          silenceAfterSpeechMs = 0;
        } else if (heardSpeech) {
          silenceAfterSpeechMs += 80;
          if (silenceAfterSpeechMs >= 1350) {
            stopMicCaptureOnly(false);
          }
        }
        if (totalElapsedMs >= 15000 && heardSpeech) {
          stopMicCaptureOnly(false);
        }
      }, 80);
    }

    mr.start();
    if (recBtn) recBtn.textContent = "⏹️ Enviar lo que dije";
    document.getElementById("voice-status-text").textContent =
      "🎤 Escuchándote… habla ahora (se envía solo al hacer una pausa o pulsa Enviar)";
  } catch (err) {
    document.getElementById("voice-status-text").textContent =
      "⚠️ Sin acceso al micrófono: puedes escribir en el cuadro de abajo y escucharás a Sofía.";
  }
}

async function triggerHitl(contactId, action) {
  let customMessage = "";
  if (action === "SEND_HUMAN_REPLY") {
    customMessage = prompt("Escribe tu mensaje directo como cerrador humano (el bot seguirá en silencio):", "Hola, te escribe el Director Comercial. Recibí tu caso, hablemos 2 minutos para apartarte el lote.") || "";
    if (!customMessage) return;
  }
  await fetch("/api/v1/hitl/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ contact_id: contactId, action, custom_message: customMessage }),
  });
  await refreshSnapshot();
}

async function runDeadLeadsReactivation() {
  const btn = document.getElementById("btn-reactivate");
  btn.disabled = true;
  btn.textContent = "Reactivando y sincronizando GHL…";
  await fetch("/api/v1/reactivation/run", { method: "POST" });
  btn.disabled = false;
  btn.textContent = "✓ Base Reactivada";
  await refreshSnapshot();
}

async function verifyGhlLive() {
  const res = await fetch("/api/v1/ghl/verify", { method: "POST" });
  const data = await res.json();
  alert(`Estado GoHighLevel API v2:\nConectado en vivo: ${data.connected}\nDetalle: ${data.detail}`);
  await refreshSnapshot();
}

async function resetDemo() {
  const btn = document.getElementById("btn-reset-demo");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Sincronizando GHL…";
  }
  try {
    await fetch("/api/v1/cockpit/reset", { method: "POST" });
    await refreshSnapshot();
    if (btn) btn.textContent = "✓ Demo + GHL Listos";
  } finally {
    setTimeout(() => {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "↺ Reiniciar Demo + GHL";
      }
    }, 1500);
  }
}

async function refreshSnapshot() {
  const res = await fetch("/api/v1/cockpit/snapshot");
  const snap = await res.json();
  state.snapshot = snap;

  // Status pill
  const ghlPill = document.getElementById("ghl-status-pill");
  if (snap.ghl.live_connected) {
    ghlPill.className = "pill live";
    ghlPill.textContent = `🟢 GHL API v2 LIVE (${snap.ghl.location_id_masked})`;
  } else {
    ghlPill.className = "pill preview";
    ghlPill.textContent = "🟡 GHL v2: Sandbox Preview (Listo para Token)";
  }

  // Buyer chat box for current phone (keep clean welcome screen until current phone speaks/chats)
  const currentPhone = (document.getElementById("buyer-phone").value || "").replace(/[^\d+]/g, "");
  const activeContact = snap.contacts.find((c) => c.phone === currentPhone);
  const chatBox = document.getElementById("chat-box");
  if (!activeContact) {
    chatBox.innerHTML = `<div class="msg AI_SOFIA"><div class="msg-role" style="display:flex;justify-content:space-between;align-items:center;"><span>🤖 Sofía IA · Horizon Estates</span><button class="btn-xs" style="padding:0.08rem 0.45rem;font-size:0.68rem;" onclick="unlockBrowserAudio();playSofiaMp3('','Hola, soy Sofía, asistente de Horizon Estates en Apple Valley. Todos nuestros lotes cuentan con financiamiento directo con ITIN o Pasaporte sin bancos. ¿Qué terreno o presupuesto de enganche buscas?')">🔊 Escuchar</button></div>¡Hola! Soy Sofía, asistente de Horizon Estates en Apple Valley, CA. Todos nuestros lotes cuentan con financiamiento directo con ITIN o Pasaporte sin bancos. ¿Qué terreno o presupuesto de enganche buscas?</div>`;
  } else {
    const msgs = snap.messages.filter((m) => m.contact_id === activeContact.id);
    chatBox.innerHTML = msgs
      .map(
        (m) => `
      <div class="msg ${m.sender_role}">
        <div class="msg-role" style="display:flex;justify-content:space-between;align-items:center;gap:0.4rem;">
          <span>${m.sender_role === "BUYER" ? "👤 Comprador" : m.sender_role === "HUMAN_ADVISOR" ? "👔 Vendedor Humano (GHL)" : "🤖 Sofía IA"} · ${m.channel}</span>
          ${m.sender_role !== "BUYER" ? `<button class="btn-xs" style="padding:0.08rem 0.45rem;font-size:0.68rem;" onclick="speakMessageById('${m.id}')">🔊 Escuchar</button>` : ""}
        </div>
        <div>${m.body}</div>
      </div>`
      )
      .join("");
    chatBox.scrollTop = chatBox.scrollHeight;

    const bPill = document.getElementById("buyer-hitl-pill");
    if (activeContact.hitl_state === "HUMAN_TAKEN_OVER") {
      bPill.className = "pill preview";
      bPill.textContent = "✋ Vendedor Humano Activo (Bot Silenciado)";
    } else if (activeContact.hitl_state === "PENDING_HITL") {
      bPill.className = "pill preview";
      bPill.textContent = "⚠️ Esperando Aprobación Gerencial HITL";
    } else {
      bPill.className = "pill live";
      bPill.textContent = "🤖 IA Autónoma Activa";
    }
  }

  // Pending HITL badge
  const pendingCount = snap.hitl_requests.filter((h) => h.status === "PENDING").length;
  const badge = document.getElementById("hitl-badge");
  badge.textContent = pendingCount;
  badge.classList.toggle("hidden", pendingCount === 0);

  // Kanban columns
  const stages = ["NEW_LEAD", "QUALIFIED_ITIN", "PENDING_HITL_APPROVAL", "SHOWING_BOOKED"];
  stages.forEach((st) => {
    const list = snap.contacts.filter((c) => c.pipeline_stage === st);
    document.getElementById(`count-${st}`).textContent = list.length;
    document.getElementById(`col-${st}`).innerHTML = list
      .map((c) => {
        const cls =
          c.hitl_state === "PENDING_HITL"
            ? "hitl-pending"
            : c.hitl_state === "HUMAN_TAKEN_OVER"
            ? "human-locked"
            : "";
        return `
        <div class="deal-card ${cls}">
          <div style="font-weight:700;">${c.full_name}</div>
          <div style="color:var(--text-muted);font-size:0.74rem;">${c.phone} · ${c.interested_lot_code} · ${c.doc_type}</div>
          <div style="margin-top:0.25rem;">Enganche: <strong>${fmtUsd(c.down_payment_usd)}</strong></div>
          <div style="font-size:0.7rem;color:#93c5fd;margin-top:0.2rem;">GHL ID: <code>${c.ghl_contact_id || "pending"}</code> (${c.ghl_last_latency_ms}ms)</div>
          <div style="margin-top:0.3rem;display:flex;flex-wrap:wrap;gap:0.25rem;">
            ${(c.tags || []).map((t) => `<span class="pill" style="padding:0.08rem 0.4rem;font-size:0.66rem;">${t}</span>`).join("")}
          </div>
          <div class="deal-actions">
            ${c.hitl_state === "PENDING_HITL" ? `<button class="btn-xs approve" onclick="triggerHitl('${c.id}','APPROVE_EXCEPTION')">✅ Aprobar Excepción</button>` : ""}
            ${c.hitl_state !== "HUMAN_TAKEN_OVER" ? `<button class="btn-xs takeover" onclick="triggerHitl('${c.id}','TAKE_OVER')">✋ Tomar Control</button>` : ""}
            <button class="btn-xs takeover" onclick="triggerHitl('${c.id}','SEND_HUMAN_REPLY')">💬 Responder como Humano</button>
            ${c.hitl_state === "HUMAN_TAKEN_OVER" ? `<button class="btn-xs release" onclick="triggerHitl('${c.id}','RELEASE_TO_AI')">🤖 Devolver a IA</button>` : ""}
          </div>
        </div>`;
      })
      .join("");
  });

  // Dead leads
  document.getElementById("dead-leads-list").innerHTML = snap.dead_leads
    .map(
      (dl) => `
    <div class="log-item">
      <strong>${dl.processed ? "✅" : "⏳"} ${dl.full_name}</strong> (${dl.origin_channel}) — Ahorro est: ${fmtUsd(dl.estimated_down_usd)}
      <div style="color:var(--text-muted);">${dl.last_note}</div>
    </div>`
    )
    .join("");

  // CRM Sync Inspector
  document.getElementById("crm-sync-logs").innerHTML =
    snap.crm_sync_events
      .map(
        (ev) => `
      <div class="log-item">
        [${ev.adapter_name}] <strong>${ev.mode}</strong> · HTTP ${ev.http_status} (${ev.latency_ms}ms) → <code>${ev.external_id}</code>
      </div>`
      )
      .join("") || `<div class="log-item">Sin eventos aún. Escribe en el chat o inicia llamada para disparar Dual-Write.</div>`;

  // Voice calls
  document.getElementById("voice-calls-log").innerHTML =
    snap.voice_calls
      .map(
        (vc) => `
      <div class="log-item">
        <strong>🎙️ [${vc.provider.toUpperCase()}]</strong> ${vc.duration_seconds}s · CA § 632 Segundo 0: ${vc.ca_632_disclosure_played ? "✅ Sí" : "❌"} · Call ID: <code>${vc.external_call_id}</code>
        <div style="color:var(--text-muted);margin-top:0.2rem;">${vc.summary_es}</div>
      </div>`
      )
      .join("") || `<div class="log-item">Aún no hay llamadas cerradas en esta sesión.</div>`;
}

// Wire calculator controls
document.getElementById("down-slider").addEventListener("input", (e) => {
  state.downUsd = Number(e.target.value);
  document.getElementById("down-label").textContent = `${fmtUsd(state.downUsd)} USD`;
  updateQuote();
});
document.querySelectorAll("#term-buttons .chip-btn").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#term-buttons .chip-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.termMonths = Number(b.dataset.term);
    updateQuote();
  });
});
document.querySelectorAll("#doc-buttons .chip-btn").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#doc-buttons .chip-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.docType = b.dataset.doc;
    updateQuote();
  });
});

async function checkPinGate() {
  try {
    const res = await fetch("/api/v1/auth/pin-status");
    const data = await res.json();
    const modal = document.getElementById("pin-gate-modal");
    if (data.pin_required && sessionStorage.getItem("demo_pin_ok") !== "1") {
      modal.classList.remove("hidden");
    }
  } catch (_) {}
}

async function submitPinGate(e) {
  e.preventDefault();
  unlockBrowserAudio();
  const pin = document.getElementById("pin-gate-input").value.trim();
  const errEl = document.getElementById("pin-gate-error");
  const res = await fetch("/api/v1/auth/verify-pin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin }),
  });
  if (res.ok) {
    sessionStorage.setItem("demo_pin_ok", "1");
    document.getElementById("pin-gate-modal").classList.add("hidden");
    errEl.classList.add("hidden");
  } else {
    errEl.classList.remove("hidden");
  }
}

// SSE live subscription
const evtSource = new EventSource("/api/v1/events/stream");
evtSource.onmessage = () => refreshSnapshot();

checkPinGate().then(loadLots).then(refreshSnapshot);
