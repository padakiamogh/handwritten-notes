/* Handwritten Notes - front end

   One EventSource per job. The server replays a snapshot on connect, so
   reopening a finished job works without any extra bookkeeping. */

const $ = (id) => document.getElementById(id);

const el = {
  tabs: document.querySelectorAll(".tab"),
  fields: document.querySelectorAll(".field"),
  begin: $("begin"),
  railError: $("rail-error"),
  file: $("file"),
  fileName: $("file-name"),
  jobSource: $("job-source"),
  jobMeta: $("job-meta"),
  spine: $("spine"),
  gate: $("gate"),
  gateLabel: $("gate-label"),
  gateQuestion: $("gate-question"),
  gateUrls: $("gate-urls"),
  gateYes: $("gate-yes"),
  gateNo: $("gate-no"),
  gateNote: $("gate-note"),
  log: $("log"),
  deliver: $("deliver"),
  dlPdf: $("dl-pdf"),
  dlHtml: $("dl-html"),
  pdfDetail: $("pdf-detail"),
  htmlDetail: $("html-detail"),
  failed: $("failed"),
  failedText: $("failed-text"),
  cancel: $("cancel"),
  jobList: $("job-list"),
  healthDot: $("health-dot"),
  healthText: $("health-text"),
  overlay: $("overlay"),
  overlayFrame: $("preview-frame"),
  overlayPdf: $("overlay-pdf"),
  overlayClose: $("overlay-close"),
};

const STAGES = [
  ["download", "Downloading"],
  ["extract", "Reading"],
  ["transcribe", "Transcribing"],
  ["research", "Researching"],
  ["notes", "Writing notes"],
  ["diagrams", "Diagrams"],
  ["pdf", "PDF"],
  ["preview", "Preview"],
];

const state = {
  mode: "youtube",
  jobId: null,
  source: new URLSearchParams(location.search).get("source") || "",
  stream: null,
  jobs: [],
};

/* ---------------------------------------------------------------- tabs */

function setMode(mode) {
  state.mode = mode;
  el.tabs.forEach((tab) => tab.classList.toggle("is-on", tab.dataset.mode === mode));
  el.fields.forEach((field) => {
    field.hidden = field.dataset.for !== mode;
  });
  el.railError.hidden = true;
  el.begin.disabled = false;
}

el.tabs.forEach((tab) => tab.addEventListener("click", () => setMode(tab.dataset.mode)));

/* ------------------------------------------------------------- health */

function describeHealth(health) {
  const missing = [];
  if (!health.ollama) missing.push("ollama/llama3");
  if (!health.ffmpeg) missing.push("ffmpeg");
  if (!health.mmdc) missing.push("mmdc");
  el.healthDot.dataset.ok = String(missing.length === 0);
  el.healthText.textContent = missing.length
    ? `needs ${missing.join(", ")}`
    : "all tools ready";
}

fetch("/api/health")
  .then((r) => r.json())
  .then(describeHealth)
  .catch(() => {
    el.healthDot.dataset.ok = "false";
    el.healthText.textContent = "server unreachable";
  });

/* ------------------------------------------------------------ the log */

const atBottom = () => el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 40;

function writeLine(text, tone) {
  const stick = atBottom();
  const span = document.createElement("span");
  if (tone) span.className = tone;
  span.textContent = text + "\n";
  el.log.appendChild(span);
  while (el.log.childNodes.length > 300) el.log.removeChild(el.log.firstChild);
  if (stick) el.log.scrollTop = el.log.scrollHeight;
}

function resetLog() {
  el.log.textContent = "";
}

/* -------------------------------------------------------------- spine */

function renderSpine(activeStage) {
  // Only show the phases a given run can actually reach, so the spine reads
  // as this job's plan rather than a generic checklist.
  el.spine.textContent = "";
  const reached = activeStage ? STAGES.findIndex((s) => s[0] === activeStage) : -1;
  const show = activeStage && ["download", "transcribe", "research"].includes(activeStage)
    ? STAGES.slice(0, 4)
    : STAGES.slice(3);
  void reached;

  show.forEach(([key, label]) => {
    const item = document.createElement("li");
    item.textContent = label;
    if (activeStage === key) item.dataset.s = "active";
    el.spine.appendChild(item);
  });
}

/* ---------------------------------------------------------------- gate */

function renderGate(pending) {
  if (!pending) {
    el.gate.hidden = true;
    return;
  }
  el.gate.hidden = false;
  el.gateQuestion.textContent = pending.question || "";
  el.gateLabel.textContent =
    pending.key === "gate_fetch" ? "Permission to read pages" : "Permission to search";

  const urls = pending.urls || [];
  if (urls.length) {
    el.gateUrls.hidden = false;
    el.gateUrls.textContent = "";
    urls.forEach((url) => {
      const item = document.createElement("li");
      item.textContent = url;
      el.gateUrls.appendChild(item);
    });
  } else {
    el.gateUrls.hidden = true;
  }

  const readAll = pending.key === "gate_fetch";
  el.gateYes.textContent = readAll ? "Read them" : "Search";
  el.gateNo.textContent = readAll ? "Read none" : "Don't search";
  el.gateNote.textContent = readAll
    ? "Decline and the job stops - there would be no source to work from."
    : "Declining is the default and always safe.";
}

async function answer(key, value) {
  if (!state.jobId) return;
  try {
    await fetch(`/api/jobs/${state.jobId}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, answer: value }),
    });
  } catch (err) {
    el.railError.hidden = false;
    el.railError.textContent = `Could not send that answer: ${err}`;
  }
}

el.gateYes.addEventListener("click", () => {
  const key = state.pendingKey;
  renderGate(null);
  answer(key, "y");
});
el.gateNo.addEventListener("click", () => {
  const key = state.pendingKey;
  renderGate(null);
  answer(key, "n");
});

/* --------------------------------------------------------- deliverables */

function humanSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

function renderDeliver(artifacts) {
  const pdf = artifacts && artifacts.pdf;
  const html = artifacts && artifacts.html;
  if (!pdf && !html) {
    el.deliver.hidden = true;
    return;
  }
  el.deliver.hidden = false;

  if (pdf) {
    el.dlPdf.href = pdf.url;
    el.dlPdf.hidden = false;
    el.pdfDetail.textContent = [
      pdf.pages ? `${pdf.pages} pages` : "",
      humanSize(pdf.size),
    ].filter(Boolean).join(" · ");
  } else {
    el.dlPdf.hidden = true;
  }

  if (html) {
    el.dlHtml.href = html.url;
    el.dlHtml.hidden = false;
    el.htmlDetail.textContent = humanSize(html.size);
    el.overlayPdf.href = pdf ? pdf.url : html.url;
  } else {
    el.dlHtml.hidden = true;
  }
}

/* ----------------------------------------------------------- job list */

function renderJobList() {
  el.jobList.textContent = "";
  if (!state.jobs.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Nothing yet.";
    el.jobList.appendChild(empty);
    return;
  }
  state.jobs.forEach((job) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.className = "job" + (job.id === state.jobId ? " is-on" : "");
    button.type = "button";

    const mark = document.createElement("span");
    mark.className = "job-mark";
    mark.dataset.s = job.status;

    const name = document.createElement("span");
    name.className = "job-name";
    name.textContent = job.source || "(upload)";

    button.append(mark, name);
    button.dataset.jobId = job.id;
    button.title = job.source || "(upload)";
    button.addEventListener("click", () => attach(job.id, job.source));
    item.appendChild(button);
    el.jobList.appendChild(item);
  });
}

async function refreshJobs() {
  try {
    const data = await (await fetch("/api/jobs")).json();
    state.jobs = data.jobs || [];
    renderJobList();
  } catch (err) {
    /* the list is a convenience; a failure here must not break the page */
  }
}

/* --------------------------------------------------------------- events */

function applySnapshot(snap) {
  el.jobSource.textContent = snap.source || "Handwritten Notes";
  el.jobMeta.textContent = describeMeta(snap);
  renderSpine(snap.stage);
  renderGate(snap.pending);
  if (snap.pending) state.pendingKey = snap.pending.key;
  renderDeliver(snap.artifacts);
  el.failed.hidden = snap.status !== "failed" && snap.status !== "cancelled";
  if (!el.failed.hidden) el.failedText.textContent = snap.error || "Stopped.";
  el.cancel.hidden = snap.status !== "running" && snap.status !== "waiting";
}

function describeMeta(snap) {
  if (snap.status === "running" && snap.stageLabel) return snap.stageLabel + "…";
  if (snap.status === "waiting") return "waiting for your decision";
  if (snap.status === "done") {
    const pdf = snap.artifacts && snap.artifacts.pdf;
    return pdf && pdf.pages ? `${pdf.pages} pages ready` : "ready";
  }
  if (snap.status === "failed" || snap.status === "cancelled") return snap.error || "stopped";
  return "Pick a source on the left to begin.";
}

function handle(event) {
  switch (event.type) {
    case "snapshot":
      applySnapshot(event);
      if (event.log) {
        resetLog();
        event.log.forEach((line) => writeLine(line, toneFor(line)));
      }
      refreshJobs();
      break;

    case "started":
      el.jobMeta.textContent = "starting…";
      break;

    case "stage":
      renderSpine(event.stage);
      if (event.label) el.jobMeta.textContent = event.label + "…";
      break;

    case "log":
      writeLine(event.line, toneFor(event.line));
      break;

    case "waiting":
      state.pendingKey = event.pending.key;
      renderGate(event.pending);
      el.jobMeta.textContent = "waiting for your decision";
      break;

    case "resumed":
      el.jobMeta.textContent = "working…";
      break;

    case "artifact":
      el.jobMeta.textContent = "writing ink…";
      break;

    case "done":
      el.jobMeta.textContent = describeMeta(event);
      renderDeliver(event.artifacts);
      renderSpine(null);
      renderGate(null);
      el.deliver.hidden = false;
      el.failed.hidden = true;
      el.cancel.hidden = true;
      refreshJobs();
      break;

    case "failed":
      el.failed.hidden = false;
      el.failedText.textContent = event.error || "The job stopped.";
      el.jobMeta.textContent = event.error || "stopped";
      renderGate(null);
      el.cancel.hidden = true;
      refreshJobs();
      break;
  }
}

function toneFor(line) {
  if (/^(Error|Could not|Failed|failed|cancelled|Traceback|  Download failed)/i.test(line)) {
    return "t-bad";
  }
  if (/^(Done|  PDF |  Pages |  Size )/.test(line)) return "t-ok";
  if (/^\s{2,}/.test(line)) return "t-muted";
  return "";
}

function attach(jobId, source) {
  if (state.stream) state.stream.close();
  state.jobId = jobId;
  state.pendingKey = null;

  if (source) el.jobSource.textContent = source;
  resetLog();
  renderGate(null);
  renderDeliver(null);
  el.failed.hidden = true;
  el.deliver.hidden = true;
  el.spine.textContent = "";
  el.jobMeta.textContent = "connecting…";

  const stream = new EventSource(`/api/jobs/${jobId}/events`);
  state.stream = stream;
  stream.onmessage = (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch (err) {
      return;
    }
    handle(event);
    if (event.type === "done" || event.type === "failed") stream.close();
  };
  stream.onerror = () => stream.close();

  renderJobList();
}

/* ---------------------------------------------------------------- start */

async function begin() {
  el.railError.hidden = true;
  el.begin.disabled = true;
  try {
    let kind = state.mode;
    let source = "";

    if (state.mode === "youtube") {
      source = $("yt").value.trim();
      if (!source) return railError("Paste a YouTube link or playlist URL.");
    } else if (state.mode === "topic") {
      source = $("topic").value.trim();
      if (!source) return railError("Name a topic to cover.");
    } else {
      const file = el.file.files[0];
      if (!file) return railError("Choose a media or document file.");
      kind = "upload";
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/upload", { method: "POST", body: form });
      if (!response.ok) throw new Error((await response.json()).detail || "upload failed");
      source = (await response.json()).path;
    }

    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, source }),
    });
    if (!response.ok) {
      throw new Error((await response.json()).detail || "could not start the job");
    }
    const job = await response.json();
    refreshJobs();
    attach(job.id, job.source);
  } catch (err) {
    railError(String(err.message || err));
  } finally {
    el.begin.disabled = false;
  }
}

function railError(message) {
  el.railError.hidden = false;
  el.railError.textContent = message;
}

el.begin.addEventListener("click", begin);
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && document.activeElement.tagName === "INPUT"
      && document.activeElement.type !== "file") {
    begin();
  }
});

/* -------------------------------------------------------------- cancel */

el.cancel.addEventListener("click", async () => {
  if (!state.jobId) return;
  await fetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
});

/* ------------------------------------------------------------- preview */

el.dlHtml.addEventListener("click", (event) => {
  event.preventDefault();
  el.overlayFrame.src = el.dlHtml.href;
  el.overlay.hidden = false;
});
el.overlayClose.addEventListener("click", () => {
  el.overlay.hidden = true;
  el.overlayFrame.src = "about:blank";
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !el.overlay.hidden) el.overlayClose.click();
});

el.file.addEventListener("change", () => {
  const file = el.file.files[0];
  el.fileName.textContent = file ? file.name : "No file chosen yet.";
});

/* ----------------------------------------------------------------- boot */

setMode("youtube");
refreshJobs();
if (state.source) $("yt").value = state.source;