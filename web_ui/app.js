/* Clinical Document Processor - UI logic.
   Talks to Python exclusively via window.pywebview.api (no network requests).
   All user-controlled strings are inserted with textContent (never innerHTML). */

"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  files: new Map(),          // name -> {tr, badgeCell, status, pages, sizeMb}
  keywords: { global: [], perFile: {} },
  selectedFile: null,
  isProcessing: false,
  pendingCount: 0,
  lastSummary: null,
  settingsCache: null,
};

/* ---------------- helpers ---------------- */

function api() { return window.pywebview.api; }

function toast(message, isInfo = false) {
  const t = $("toast");
  t.textContent = message;
  t.classList.toggle("info", isInfo);
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 4200);
}

function apiCall(promise) {
  return promise.catch((err) => {
    const msg = (err && err.message) ? err.message : String(err);
    toast(msg.replace(/^.*?Error:\s*/, ""));
    throw err;
  });
}

function fmtTime(totalSeconds) {
  if (totalSeconds == null || totalSeconds < 0) return "…";
  const s = Math.floor(totalSeconds % 60);
  const m = Math.floor((totalSeconds / 60) % 60);
  const h = Math.floor(totalSeconds / 3600);
  return `${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
}

const BADGES = {
  pending:    { text: "Pending",     cls: "badge-muted" },
  processing: { text: "Processing",  cls: "badge-accent badge-processing" },
  success:    { text: "Success",     cls: "badge-ok" },
  verified:   { text: "✔ Verified",  cls: "badge-ok" },
  review:     { text: "⚠ Review",    cls: "badge-warn" },
  failed:     { text: "✖ Failed",    cls: "badge-danger" },
  completed:  { text: "Completed",   cls: "badge-muted" },
};

/* ---------------- posture strip ---------------- */

function renderPosture(p) {
  const strip = $("postureStrip");
  strip.replaceChildren();

  const mk = (text, cls) => {
    const b = document.createElement("span");
    b.className = `badge ${cls}`;
    b.textContent = text;
    strip.appendChild(b);
  };

  mk(`Region: ${p.region}`, "badge-accent");
  mk(p.redaction ? `Redaction: ON ×${p.redaction_iterations}` : "Redaction: OFF!", p.redaction ? "badge-ok" : "badge-danger");
  mk(p.translation ? `Translation → ${p.translation_region} (US)` : "Translation: OFF", p.translation ? "badge-warn" : "badge-muted");
  mk(p.verify ? "Verify: ON" : "Verify: OFF", p.verify ? "badge-ok" : "badge-warn");
  mk(p.audit ? "Audit: ON" : "Audit: OFF", p.audit ? "badge-ok" : "badge-warn");
}

/* ---------------- files table ---------------- */

function clearFiles() {
  state.files.clear();
  state.selectedFile = null;
  $("filesBody").replaceChildren();
  $("emptyState").classList.add("hidden");
}

function addFileRow(name, status, pages, sizeMb) {
  const tr = document.createElement("tr");

  const tdName = document.createElement("td");
  const nameSpan = document.createElement("span");
  nameSpan.textContent = name;
  const tagCount = document.createElement("span");
  tagCount.className = "tag-count hidden";
  tdName.append(nameSpan, tagCount);

  const tdPages = document.createElement("td");
  tdPages.className = "num";
  tdPages.textContent = pages != null ? pages : "—";

  const tdSize = document.createElement("td");
  tdSize.className = "num";
  tdSize.textContent = sizeMb != null ? `${sizeMb} MB` : "—";

  const tdBadge = document.createElement("td");

  tr.append(tdName, tdPages, tdSize, tdBadge);
  tr.addEventListener("click", () => selectFile(name));
  $("filesBody").appendChild(tr);

  const entry = { tr, badgeCell: tdBadge, tagCountEl: tagCount, status, pages, sizeMb };
  state.files.set(name, entry);
  setFileStatus(name, status);
  updateTagIndicators();
}

function updateTagIndicators() {
  // Per-document tag counters in the file list ("each document has its own tags")
  for (const [name, entry] of state.files) {
    const n = (state.keywords.perFile[name] || []).length;
    entry.tagCountEl.textContent = n ? `🏷 ${n}` : "";
    entry.tagCountEl.classList.toggle("hidden", n === 0);
  }
}

function setFileStatus(name, status) {
  const entry = state.files.get(name);
  if (!entry) return;
  entry.status = status;

  const spec = BADGES[status] || BADGES.pending;
  const badge = document.createElement("span");
  badge.className = `badge ${spec.cls}`;
  badge.textContent = spec.text;
  entry.badgeCell.replaceChildren(badge);

  if (status === "processing") {
    entry.tr.scrollIntoView({ block: "nearest" });
  }
  // The document just finished while on screen -> show its anonymized result
  if (tagState.name === name && ["success", "verified", "review"].includes(status)) {
    showInViewer(name, "result");
  }
}

function updateCounts() {
  let pending = 0, done = 0, failed = 0;
  for (const { status } of state.files.values()) {
    if (["pending", "processing"].includes(status)) pending++;
    else if (status === "failed") failed++;
    else done++;
  }
  state.pendingCount = pending;
  const parts = [`${pending} pending`, `${done} done`];
  if (failed) parts.push(`${failed} failed`);
  $("fileCounts").textContent = parts.join(" · ");
  $("btnStart").disabled = state.isProcessing || pending === 0;
}

/* ---------------- keywords (chips) ---------------- */

function selectFile(name) {
  state.selectedFile = name;
  for (const [n, entry] of state.files) {
    entry.tr.classList.toggle("selected", n === name);
  }
  showInViewer(name);
}

function addKeyword() {
  const input = $("kwInput");
  const val = input.value.replace(/,/g, "").trim();
  input.value = "";
  if (!val) return;

  const wantFile = $("tagTarget").value === "file";
  const target = wantFile ? tagState.name : null;
  if (wantFile && !target) {
    toast("Click a document first to add one of its tags.", true);
    return;
  }

  if (target) {
    const list = state.keywords.perFile[target] || (state.keywords.perFile[target] = []);
    if (!list.includes(val)) list.push(val);
  } else if (!state.keywords.global.includes(val)) {
    state.keywords.global.push(val);
  }
  renderWordBoxes();  // refreshes highlights, chips and tag counters
}

/* ---------------- log & progress ---------------- */

function appendLog(ts, message) {
  const pane = $("logPane");
  const atBottom = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 8;
  pane.textContent += `${ts} - ${message}\n`;
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function renderProgress(ev) {
  const pct = ev.pages_total > 0 ? Math.min(100, (ev.pages_done / ev.pages_total) * 100) : 0;
  $("progressFill").style.width = `${pct}%`;
  const remaining = ev.calibrated ? `Est. remaining: ${fmtTime(ev.remaining)}` : "Est. remaining: calibrating…";
  $("progressText").textContent =
    `${ev.pages_done}/${ev.pages_total} pages · Elapsed: ${fmtTime(ev.elapsed)} · ${remaining}`;
}

/* ---------------- event pump ---------------- */

function handleEvent(ev) {
  switch (ev.type) {
    case "log":
      appendLog(ev.ts, ev.message);
      break;
    case "scan_start":
      clearFiles();
      clearViewer();
      $("sourcePath").textContent = ev.folder;
      $("outputPath").textContent = ev.output_folder;
      break;
    case "scan_item":
      addFileRow(ev.name, ev.status, ev.pages, ev.size_mb);
      updateCounts();
      break;
    case "scan_done":
      updateCounts();
      if (state.files.size === 0) $("emptyState").classList.remove("hidden");
      break;
    case "file_status":
      setFileStatus(ev.name, ev.status);
      updateCounts();
      break;
    case "progress":
      renderProgress(ev);
      break;
    case "env":
      $("envInfo").textContent = `GPU: ${ev.gpu} · Ping: ${ev.ping} ms`;
      break;
    case "batch_started":
      state.isProcessing = true;
      $("btnStart").disabled = true;
      $("btnStop").disabled = false;
      break;
    case "batch_finished":
      state.isProcessing = false;
      $("btnStop").disabled = true;
      updateCounts();
      break;
    case "summary":
      showSummary(ev);
      break;
    case "sync_warning": {
      const parts = ev.warnings.map((w) => `the ${w.where} is inside a ${w.service}-synced location`);
      $("syncBannerText").textContent =
        `☁⚠ Careful: ${parts.join(" and ")} — synced files are copied to the cloud outside this app's control. ` +
        `Consider a local, non-synced folder for clinical originals.`;
      $("syncBanner").classList.remove("hidden");
      break;
    }
    case "error":
      toast(ev.message);
      break;
  }
}

async function pump() {
  try {
    const events = await api().poll_events();
    for (const ev of events) handleEvent(ev);
  } catch (e) { /* bridge not ready yet */ }
}

/* ---------------- summary modal ---------------- */

function showSummary(ev) {
  state.lastSummary = ev;
  $("summaryHeadline").textContent = `${ev.success} of ${ev.total} documents processed successfully`;
  $("summaryTime").textContent = `Time: ${fmtTime(ev.elapsed)}`;
  $("summaryFolder").textContent = `Saved to: ${ev.output_folder}`;

  const lists = $("summaryLists");
  lists.replaceChildren();

  const mkList = (items, label, badgeCls) => {
    if (!items.length) return;
    const wrap = document.createElement("div");
    wrap.className = "summary-list";
    for (const name of items) {
      const row = document.createElement("div");
      const b = document.createElement("span");
      b.className = `badge ${badgeCls}`;
      b.textContent = label;
      const n = document.createElement("span");
      n.textContent = name;
      row.append(b, n);
      wrap.appendChild(row);
    }
    lists.appendChild(wrap);
  };

  mkList(ev.review, "⚠ Review", "badge-warn");
  mkList(ev.failed, "✖ Failed", "badge-danger");

  if (!ev.review.length && !ev.failed.length) {
    const ok = document.createElement("p");
    ok.className = "summary-headline";
    ok.style.color = "var(--ok)";
    ok.textContent = "✔ All outputs saved and verified.";
    lists.appendChild(ok);
  }

  $("btnSummaryRetry").classList.toggle("hidden", !ev.failed.length);
  $("summaryModal").classList.remove("hidden");
}

/* ---------------- click-to-tag viewer ---------------- */

const SERVER_ZOOM = 2.0;  // render resolution; on-screen size is CSS-scaled for instant zoom

const tagState = {
  mode: "original",        // "original" (click-to-tag) | "result" (anonymized output)
  name: null, page: 0, pageCount: 1,
  scale: 1, autoFit: true, pageW: 0, pageH: 0,
  words: [], hasText: false, ocrDone: false, ocrAvailable: false,
  ocrConsent: new Set(),   // documents where the user already approved Cloud OCR
  loading: false,
};

function cleanWord(text) {
  // Strip punctuation from the edges, keep letters/digits (Unicode-aware: é, ü, ß…)
  return (text || "").replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
}

function taggedSetFor(name) {
  const set = new Set(state.keywords.global.map((k) => k.toLowerCase()));
  for (const k of (state.keywords.perFile[name] || [])) set.add(k.toLowerCase());
  return set;
}

function clearViewer() {
  tagState.name = null;
  $("viewerTitle").textContent = "Document viewer";
  $("tagSizer").classList.add("hidden");
  $("viewerEmpty").classList.remove("hidden");
  $("ocrNotice").classList.add("hidden");
  $("tagPageLabel").textContent = "– / –";
  $("tagStatus").textContent = "";
  $("btnModeOriginal").classList.remove("active");
  $("btnModeResult").classList.remove("active");
  $("tagChips").replaceChildren();
}

async function showInViewer(name, forcedMode) {
  const entry = state.files.get(name);
  const status = entry ? entry.status : "pending";
  const mode = forcedMode ||
    (["success", "verified", "review", "completed"].includes(status) ? "result" : "original");
  if (tagState.name !== name) {
    tagState.page = 0;
    tagState.autoFit = true;
  }
  tagState.name = name;
  tagState.mode = mode;
  await loadViewerPage();
}

function setViewerMode(mode) {
  if (!tagState.name) return;
  showInViewer(tagState.name, mode);
}

function applyScale() {
  const s = tagState.scale;
  $("tagSizer").style.width = `${Math.round(tagState.pageW * s)}px`;
  $("tagSizer").style.height = `${Math.round(tagState.pageH * s)}px`;
  $("tagStage").style.transform = `scale(${s})`;
}

function fitWidth() {
  if (!tagState.pageW) return;
  const vp = $("tagViewport");
  tagState.scale = Math.max(0.15, Math.min(1.5, (vp.clientWidth - 22) / tagState.pageW));
  applyScale();
}

async function loadViewerPage() {
  if (tagState.loading || !tagState.name) return;
  tagState.loading = true;
  $("tagStatus").textContent = "Rendering…";
  $("btnModeOriginal").classList.toggle("active", tagState.mode === "original");
  $("btnModeResult").classList.toggle("active", tagState.mode === "result");
  $("viewerTitle").textContent = tagState.mode === "original"
    ? `📄 ${tagState.name} — original (click words to tag)`
    : `✅ ${tagState.name} — anonymized result`;
  try {
    let data;
    try {
      data = (tagState.mode === "original")
        ? await api().get_source_preview(tagState.name, tagState.page, SERVER_ZOOM)
        : await api().get_output_preview(tagState.name, tagState.page, SERVER_ZOOM);
    } catch (e) {
      const msg = (e && e.message) ? e.message : String(e);
      $("tagStatus").textContent = `⚠ Could not render: ${msg.replace(/^.*?Error:\s*/, "")}`;
      return;
    }
    if (!data) {
      if (tagState.mode === "result") {
        // No output yet -> fall back to the original transparently
        tagState.mode = "original";
        tagState.loading = false;
        $("tagStatus").textContent = "No anonymized output yet — showing the original.";
        return loadViewerPage();
      }
      $("tagStatus").textContent = "⚠ File not found in the source folder.";
      return;
    }

    tagState.page = data.page;
    tagState.pageCount = data.page_count;
    tagState.words = data.words || [];
    tagState.hasText = data.has_text_layer;
    tagState.ocrDone = data.ocr_done;
    tagState.ocrAvailable = data.ocr_available;
    tagState.pageW = data.width;
    tagState.pageH = data.height;

    $("viewerEmpty").classList.add("hidden");
    $("tagSizer").classList.remove("hidden");
    const img = $("tagPageImg");
    img.src = data.image;
    img.style.width = `${data.width}px`;
    img.style.height = `${data.height}px`;
    const stage = $("tagStage");
    stage.style.width = `${data.width}px`;
    stage.style.height = `${data.height}px`;
    if (tagState.autoFit) { tagState.autoFit = false; fitWidth(); } else { applyScale(); }

    $("tagPageLabel").textContent = `${data.page + 1} / ${data.page_count}`;
    $("tagPrev").disabled = data.page <= 0;
    $("tagNext").disabled = data.page >= data.page_count - 1;
    $("tagStatus").textContent = "";

    renderWordBoxes();
    if (tagState.mode === "original") {
      updateOcrNotice();
      tagState.loading = false;
      // Consent already given for this document -> OCR further scanned pages automatically
      if (!tagState.ocrDone && tagState.ocrAvailable && tagState.ocrConsent.has(tagState.name)) {
        await runOcrCurrentPage();
      }
    } else {
      $("ocrNotice").classList.add("hidden");
    }
  } finally {
    tagState.loading = false;
  }
}

function updateOcrNotice() {
  const needsOcr = tagState.mode === "original" && !tagState.ocrDone;
  $("ocrNotice").classList.toggle("hidden", !needsOcr);
  const btn = $("btnDetectText");
  btn.disabled = !tagState.ocrAvailable;
  btn.title = tagState.ocrAvailable ? "" : "Configure Google credentials first (see README)";
}

async function runOcrCurrentPage() {
  tagState.ocrConsent.add(tagState.name);
  $("tagStatus").textContent = "Detecting text (Cloud OCR)…";
  $("btnDetectText").disabled = true;
  try {
    await apiCall(api().ocr_source_page(tagState.name, tagState.page));
    const data = await api().get_source_preview(tagState.name, tagState.page, SERVER_ZOOM);
    tagState.words = data.words;
    tagState.ocrDone = data.ocr_done;
    $("tagStatus").textContent = "";
    renderWordBoxes();
    updateOcrNotice();
  } catch (e) {
    $("tagStatus").textContent = "Text detection failed.";
    $("btnDetectText").disabled = !tagState.ocrAvailable;
  }
}

function renderWordBoxes() {
  const stage = $("tagStage");
  stage.querySelectorAll(".wordbox").forEach((el) => el.remove());
  const tagged = taggedSetFor(tagState.name);

  for (const w of tagState.words) {
    const clean = cleanWord(w.text);
    if (clean.length < 2) continue;
    const isTagged = tagged.has(clean.toLowerCase());

    const box = document.createElement("span");
    box.className = "wordbox" + (isTagged ? " tagged" : "");
    box.style.left = `${w.x0}px`;
    box.style.top = `${w.y0}px`;
    box.style.width = `${w.x1 - w.x0}px`;
    box.style.height = `${w.y1 - w.y0}px`;
    box.title = isTagged ? `Un-tag "${clean}"` : `Tag "${clean}" for erasure`;
    box.addEventListener("click", () => toggleTagWord(clean));
    stage.appendChild(box);
  }
  renderTagChips();
}

function toggleTagWord(word) {
  const lower = word.toLowerCase();
  const name = tagState.name;
  const fileList = state.keywords.perFile[name] || [];
  const inGlobal = state.keywords.global.some((k) => k.toLowerCase() === lower);
  const inFile = fileList.some((k) => k.toLowerCase() === lower);

  if (inGlobal || inFile) {
    state.keywords.global = state.keywords.global.filter((k) => k.toLowerCase() !== lower);
    if (state.keywords.perFile[name]) {
      state.keywords.perFile[name] = fileList.filter((k) => k.toLowerCase() !== lower);
    }
  } else if ($("tagTarget").value === "file") {
    (state.keywords.perFile[name] = state.keywords.perFile[name] || []).push(word);
  } else {
    state.keywords.global.push(word);
  }
  renderWordBoxes();
}

function renderTagChips() {
  const box = $("tagChips");
  box.replaceChildren();
  $("tagChipsLabel").textContent = tagState.name ? "This document's tags:" : "Global tags:";

  const mkChip = (kw, isGlobal) => {
    const chip = document.createElement("span");
    chip.className = `chip ${isGlobal ? "chip-global" : "chip-file"}`;
    const label = document.createElement("span");
    label.textContent = isGlobal ? `${kw} (G)` : kw;
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = "Remove";
    x.addEventListener("click", () => {
      state.keywords.global = state.keywords.global.filter((k) => k !== kw);
      const fl = state.keywords.perFile[tagState.name];
      if (fl) state.keywords.perFile[tagState.name] = fl.filter((k) => k !== kw);
      renderWordBoxes();
    });
    chip.append(label, x);
    box.appendChild(chip);
  };

  // This document's own tags first; global (G) tags after
  for (const kw of (state.keywords.perFile[tagState.name] || [])) {
    if (!state.keywords.global.includes(kw)) mkChip(kw, false);
  }
  for (const kw of state.keywords.global) mkChip(kw, true);

  if (!box.children.length) {
    const none = document.createElement("span");
    none.className = "hint";
    none.textContent = "none yet — click words on the page or type one above";
    box.appendChild(none);
  }
  updateTagIndicators();
}

/* ---------------- settings modal ---------------- */

function openSettings() {
  if (state.isProcessing) { toast("Settings are locked while processing is running.", true); return; }

  apiCall(api().get_settings()).then((s) => {
    state.settingsCache = s;
    const oo = s.output_options, tr = s.translation, ap = s.app_settings;

    document.querySelectorAll('input[name="policy"]').forEach((r) => {
      r.checked = (r.value === (ap.overwrite_policy || "skip"));
    });
    $("sSelectable").checked = !!oo.selectable_text_copy;
    $("sFlattened").checked = !!oo.non_selectable_text_copy;
    $("sRedaction").checked = !!oo.redaction;
    $("sIterations").value = oo.redaction_iterations ?? 1;
    $("sTranslate").checked = !!tr.enabled;
    $("sLang").value = tr.target_language_code || "en";
    $("sTransIter").value = oo.translation_redaction_iterations ?? 0;
    $("sMergeFull").checked = !!oo.generate_full_translated_document;
    $("sVerify").checked = ap.verification_scan !== false;
    $("sAudit").checked = ap.audit_log !== false;
    $("transNote").textContent =
      `⚠ Translation is processed by Google in ${s.translation_region} (US), outside the EU. Only already-redacted copies are sent.`;

    $("settingsModal").classList.remove("hidden");
  });
}

function saveSettings() {
  if (!$("sSelectable").checked && !$("sFlattened").checked) {
    toast("Select at least one output type (selectable or flattened).");
    return;
  }
  if (!$("sRedaction").checked &&
      !confirm("Redaction is DISABLED. Output files will contain the ORIGINAL sensitive data.\n\nContinue anyway?")) {
    return;
  }

  const policy = document.querySelector('input[name="policy"]:checked');
  const payload = {
    output_options: {
      selectable_text_copy: $("sSelectable").checked,
      non_selectable_text_copy: $("sFlattened").checked,
      redaction: $("sRedaction").checked,
      redaction_iterations: parseInt($("sIterations").value, 10) || 1,
      translation_redaction_iterations: parseInt($("sTransIter").value, 10) || 0,
      generate_full_translated_document: $("sMergeFull").checked,
    },
    translation: {
      enabled: $("sTranslate").checked,
      target_language_code: $("sLang").value,
    },
    app_settings: {
      output_folder: (state.settingsCache?.app_settings?.output_folder) || "",
      overwrite_policy: policy ? policy.value : "skip",
      verification_scan: $("sVerify").checked,
      audit_log: $("sAudit").checked,
    },
  };

  apiCall(api().save_settings(payload)).then((res) => {
    renderPosture(res.posture);
    if (res.output_folder) $("outputPath").textContent = res.output_folder;
    $("settingsModal").classList.add("hidden");
    // Re-scan: output/overwrite settings change what counts as 'already done'
    refreshStateLight();
  });
}

async function refreshStateLight() {
  try {
    const st = await api().get_state();
    renderPosture(st.posture);
    if (st.output_folder) $("outputPath").textContent = st.output_folder;
  } catch (e) { /* ignore */ }
}

/* ---------------- credentials banner ---------------- */

async function checkCredentials() {
  try {
    const status = await api().credentials_status();
    if (status.state === "in_project") {
      $("credBanner").classList.remove("hidden");
    } else if (status.state === "missing") {
      appendLog("--:--:--", "Note: no service account key found yet - see the README to configure Google Cloud access.");
    }
  } catch (e) { /* ignore */ }
}

/* ---------------- wiring ---------------- */

function wire() {
  $("btnSelectFolder").addEventListener("click", () => {
    apiCall(api().choose_source_folder()).then((res) => {
      if (res) {
        $("sourcePath").textContent = res.source_folder;
        $("outputPath").textContent = res.output_folder;
      }
    });
  });

  $("btnChangeOutput").addEventListener("click", () => {
    apiCall(api().choose_output_folder()).then((res) => {
      if (res) $("outputPath").textContent = res.output_folder;
    });
  });

  $("btnResetOutput").addEventListener("click", () => {
    apiCall(api().reset_output_folder()).then((res) => {
      $("outputPath").textContent = res.output_folder || "(select a data folder first)";
    });
  });

  $("btnOpenOutput").addEventListener("click", () => apiCall(api().open_output_folder()));

  $("btnStart").addEventListener("click", () => {
    apiCall(api().start_batch({ global: state.keywords.global, per_file: state.keywords.perFile }));
  });

  $("btnStop").addEventListener("click", () => {
    if (confirm("Are you sure you want to stop the batch processing?")) {
      apiCall(api().stop_batch());
    }
  });

  $("btnAddKw").addEventListener("click", addKeyword);
  $("kwInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addKeyword(); }
  });

  // Settings
  $("btnSettings").addEventListener("click", openSettings);
  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("btnCancelSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));

  // Summary
  $("btnSummaryClose").addEventListener("click", () => $("summaryModal").classList.add("hidden"));
  $("btnSummaryOpen").addEventListener("click", () => apiCall(api().open_output_folder()));
  $("btnSummaryRetry").addEventListener("click", () => {
    apiCall(api().retry_failed()).then(() => {
      $("summaryModal").classList.add("hidden");
      updateCounts();
    });
  });

  // Persistent document viewer
  $("btnModeOriginal").addEventListener("click", () => setViewerMode("original"));
  $("btnModeResult").addEventListener("click", () => setViewerMode("result"));
  $("tagPrev").addEventListener("click", () => { if (tagState.page > 0) { tagState.page--; loadViewerPage(); } });
  $("tagNext").addEventListener("click", () => { if (tagState.page < tagState.pageCount - 1) { tagState.page++; loadViewerPage(); } });
  $("tagZoomIn").addEventListener("click", () => { tagState.scale = Math.min(2.5, tagState.scale * 1.25); applyScale(); });
  $("tagZoomOut").addEventListener("click", () => { tagState.scale = Math.max(0.15, tagState.scale * 0.8); applyScale(); });
  $("tagZoomFit").addEventListener("click", fitWidth);
  $("btnDetectText").addEventListener("click", runOcrCurrentPage);

  // Collapsible execution log
  $("logToggle").addEventListener("click", () => {
    const pane = $("logPane");
    const hidden = pane.classList.toggle("hidden");
    $("logChev").textContent = hidden ? "▸" : "▾";
    if (!hidden) pane.scrollTop = pane.scrollHeight;
  });

  // Keyboard: arrow keys turn pages when not typing in a field
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
    if (!tagState.name) return;
    if (e.key === "ArrowLeft" && !$("tagPrev").disabled) { tagState.page--; loadViewerPage(); }
    if (e.key === "ArrowRight" && !$("tagNext").disabled) { tagState.page++; loadViewerPage(); }
  });

  // Credentials banner
  $("btnMoveKey").addEventListener("click", () => {
    apiCall(api().move_credentials()).then((newPath) => {
      $("credBanner").classList.add("hidden");
      toast(`Key secured at: ${newPath}`, true);
    });
  });
  $("btnDismissCred").addEventListener("click", () => {
    $("credBanner").classList.add("hidden");
    appendLog("--:--:--", "⚠ Service account key left in app folder. Avoid zipping or sharing this folder.");
  });

  $("btnDismissSync").addEventListener("click", () => $("syncBanner").classList.add("hidden"));

  // Best-effort folder drag & drop (pywebview exposes real paths)
  const card = $("folderCard");
  card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("dragover"); });
  card.addEventListener("dragleave", () => card.classList.remove("dragover"));
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    card.classList.remove("dragover");
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    const path = f && (f.pywebviewFullPath || f.path);
    if (path) {
      apiCall(api().set_source_folder(path)).then((res) => {
        if (res) {
          $("sourcePath").textContent = res.source_folder;
          $("outputPath").textContent = res.output_folder;
        }
      });
    } else {
      toast("Drop not supported here - please use the Select Data Folder button.", true);
    }
  });

  // Close modals on backdrop click
  for (const id of ["settingsModal", "summaryModal"]) {
    $(id).addEventListener("click", (e) => {
      if (e.target === $(id)) $(id).classList.add("hidden");
    });
  }
}

async function init() {
  wire();
  try {
    const st = await api().get_state();
    $("appVersion").textContent = `v${st.app_version}`;
    renderPosture(st.posture);
    $("envInfo").textContent = `GPU: ${st.env.gpu} · Ping: ${st.env.ping} ms`;
    if (st.source_folder) $("sourcePath").textContent = st.source_folder;
    if (st.output_folder) $("outputPath").textContent = st.output_folder;
    $("emptyState").classList.remove("hidden");
  } catch (e) {
    toast("Could not reach the application backend.");
  }
  checkCredentials();
  setInterval(pump, 300);
}

window.addEventListener("pywebviewready", init);
