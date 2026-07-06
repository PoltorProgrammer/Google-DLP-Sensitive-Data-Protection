/* Clinical Document Processor - UI logic.
   Talks to Python exclusively via window.pywebview.api (no network requests).
   All user-controlled strings are inserted with textContent (never innerHTML). */

"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  files: new Map(),          // name -> {tr, badgeCell, actionCell, status, pages, sizeMb}
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
  renderKeywordUI();
}

function addFileRow(name, status, pages, sizeMb) {
  const tr = document.createElement("tr");

  const tdName = document.createElement("td");
  tdName.textContent = name;

  const tdPages = document.createElement("td");
  tdPages.className = "num";
  tdPages.textContent = pages != null ? pages : "—";

  const tdSize = document.createElement("td");
  tdSize.className = "num";
  tdSize.textContent = sizeMb != null ? `${sizeMb} MB` : "—";

  const tdBadge = document.createElement("td");
  const tdAction = document.createElement("td");

  tr.append(tdName, tdPages, tdSize, tdBadge, tdAction);
  tr.addEventListener("click", () => selectFile(name));
  $("filesBody").appendChild(tr);

  const entry = { tr, badgeCell: tdBadge, actionCell: tdAction, status, pages, sizeMb };
  state.files.set(name, entry);
  setFileStatus(name, status);
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

  entry.actionCell.replaceChildren();
  if (["success", "verified", "review", "completed"].includes(status)) {
    const btn = document.createElement("button");
    btn.className = "btn btn-small";
    btn.textContent = "Preview";
    btn.addEventListener("click", (e) => { e.stopPropagation(); openPreview(name); });
    entry.actionCell.appendChild(btn);
  }
  if (status === "processing") {
    entry.tr.scrollIntoView({ block: "nearest" });
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
  state.selectedFile = (state.selectedFile === name) ? null : name;
  for (const [n, entry] of state.files) {
    entry.tr.classList.toggle("selected", n === state.selectedFile);
  }
  renderKeywordUI();
}

function renderKeywordUI() {
  const sel = state.selectedFile;
  $("kwTitle").textContent = sel ? `Keywords for: ${sel}` : "Global redaction keywords";
  $("kwHint").textContent = sel
    ? "These keywords apply only to the selected file (click the row again to go back to global)."
    : "Names/IDs typed here are expanded into hundreds of spelling variants and burned out of every document. Select a file below to add file-specific keywords.";

  const box = $("chipContainer");
  box.replaceChildren();

  const mkChip = (kw, isGlobal) => {
    const chip = document.createElement("span");
    chip.className = `chip ${isGlobal ? "chip-global" : "chip-file"}`;
    const label = document.createElement("span");
    label.textContent = isGlobal ? `${kw} (G)` : kw;
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = "Remove";
    x.addEventListener("click", () => removeKeyword(kw));
    chip.append(label, x);
    box.appendChild(chip);
  };

  for (const kw of state.keywords.global) mkChip(kw, true);
  if (sel) {
    for (const kw of (state.keywords.perFile[sel] || [])) {
      if (!state.keywords.global.includes(kw)) mkChip(kw, false);
    }
  }
}

function addKeyword() {
  const input = $("kwInput");
  const val = input.value.replace(/,/g, "").trim();
  input.value = "";
  if (!val) return;

  if (state.selectedFile) {
    const list = state.keywords.perFile[state.selectedFile] || (state.keywords.perFile[state.selectedFile] = []);
    if (!list.includes(val)) list.push(val);
  } else if (!state.keywords.global.includes(val)) {
    state.keywords.global.push(val);
  }
  renderKeywordUI();
}

function removeKeyword(kw) {
  const sel = state.selectedFile;
  if (sel && (state.keywords.perFile[sel] || []).includes(kw)) {
    state.keywords.perFile[sel] = state.keywords.perFile[sel].filter((k) => k !== kw);
  } else {
    state.keywords.global = state.keywords.global.filter((k) => k !== kw);
  }
  renderKeywordUI();
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

/* ---------------- preview modal ---------------- */

async function openPreview(name) {
  $("previewTitle").textContent = `Preview — ${name}`;
  $("previewInfo").textContent = "Rendering…";
  $("previewPages").replaceChildren();
  $("previewModal").classList.remove("hidden");

  try {
    const data = await api().get_preview(name, 6);
    if (!data) {
      $("previewInfo").textContent = "No output file found for this document.";
      return;
    }
    $("previewInfo").textContent =
      `${data.name} — showing ${data.pages.length} of ${data.total_pages} page(s). Rendered locally; nothing leaves this machine.`;
    for (const src of data.pages) {
      const img = document.createElement("img");
      img.src = src;
      $("previewPages").appendChild(img);
    }
  } catch (e) {
    $("previewInfo").textContent = "Preview failed.";
  }
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

  // Preview
  $("btnPreviewClose").addEventListener("click", () => $("previewModal").classList.add("hidden"));

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
  for (const id of ["settingsModal", "summaryModal", "previewModal"]) {
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
  renderKeywordUI();
  checkCredentials();
  setInterval(pump, 300);
}

window.addEventListener("pywebviewready", init);
