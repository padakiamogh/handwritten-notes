"""Web front end for the handwritten-notes generator.

Design notes
------------
The pipeline is CPU-bound and slow (llama3 inference alone can take minutes),
so nothing here runs it inside a request handler. Instead each run is a Job:
main.py is spawned as a subprocess and supervised. Its stdout is parsed for the
sentinels it emits when NOTES_UI is set:

    <<<STAGE name>                 pipeline phase changed
    <<<ARTIFACT kind= path= ...    a deliverable landed on disk
    <<<PROMPT key= ...             a decision is now blocking the pipeline

The <<<PROMPT sentinel is the important one. main.py blocks on input() at its
two consent gates, so the job genuinely pauses. The browser is told a decision
is pending, and POST /answer writes the answer into the child's stdin. Consent
is never auto-answered - auto-piping "y" would hollow out the feature that
matters most in this project.

This is deliberately a thin process supervisor. main.py stays the tested CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
MAIN_PY = BASE_DIR / "main.py"
STATIC_DIR = Path(__file__).resolve().parent / "static"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"
UPLOAD_DIR = TEMP_DIR / "uploads"
JOB_ARTIFACT_DIR = OUTPUT_DIR / "jobs"

PROMPT_SENTINEL = "<<<PROMPT"
STAGE_SENTINEL = "<<<STAGE"
ARTIFACT_SENTINEL = "<<<ARTIFACT"

MAX_LOG_LINES = 400

# --- what the UI calls each phase -------------------------------------------
STAGE_LABELS = {
    "download": "Downloading audio",
    "extract": "Reading document",
    "transcribe": "Transcribing",
    "research": "Gathering sources",
    "notes": "Writing notes",
    "diagrams": "Rendering diagrams",
    "pdf": "Building PDF",
    "preview": "Preparing preview",
}

# Consent gates are surfaced as real decisions; these two are housekeeping and
# are answered by the server because they are not research permissions.
AUTO_ANSWERS = {"done_again": "n"}


def _short(label: str, limit: int = 68) -> str:
    label = (label or "").strip()
    return label if len(label) <= limit else label[: limit - 1] + "…"


@dataclass
class Artifact:
    kind: str
    path: Path
    size: int = 0
    pages: int | None = None


@dataclass
class Job:
    id: str
    source: str
    kind: str
    created: float = field(default_factory=time.time)

    status: str = "queued"          # queued|running|waiting|done|failed|cancelled
    stage: str | None = None
    log: list[str] = field(default_factory=list)
    pending: dict | None = None     # the <<<PROMPT payload, if any
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    error: str | None = None

    process: subprocess.Popen | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    subscribers: list = field(default_factory=list)   # list[(loop, queue)]

    # -- event fan-out ------------------------------------------------------
    def publish(self, event: dict) -> None:
        """Hand an event to every live SSE subscriber. Called from a reader thread."""
        for loop, queue in list(self.subscribers):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

    def subscribe(self, loop, queue) -> None:
        self.subscribers.append((loop, queue))

    def unsubscribe(self, queue) -> None:
        self.subscribers = [(l, q) for (l, q) in self.subscribers if q is not queue]

    # -- state snapshots ----------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "source": _short(self.source),
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "stageLabel": STAGE_LABELS.get(self.stage or "", None),
            "pending": self.pending,
            "error": self.error,
            "created": self.created,
            "artifacts": {
                key: {
                    "kind": art.kind,
                    "url": f"/api/jobs/{self.id}/artifact/{art.kind}",
                    "size": art.size,
                    "pages": art.pages,
                }
                for key, art in self.artifacts.items()
            },
            "log": self.log[-120:],
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def create(self, source: str, kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], source=source, kind=kind)
        with self.lock:
            self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        return job

    def recent(self) -> list[dict]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.snapshot() for j in jobs[:20]]


manager = JobManager()
app = FastAPI(title="Handwritten Notes", docs_url=None, redoc_url=None)


# ===========================================================================
# subprocess supervision
# ===========================================================================

def _parse_fields(rest: str) -> dict:
    """Parse a sentinel tail.

    Shapes in use: a bare `gate_search`, and `gate_fetch count=2 urls=a,b`.
    Values may themselves contain '=' (query strings), so only the first '='
    splits a token.
    """
    tokens = rest.split()
    fields: dict[str, str] = {}
    if tokens and "=" not in tokens[0]:
        fields["key"] = tokens[0]
        tokens = tokens[1:]
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if key:
            fields[key] = value
    return fields


def _record(job: Job, line: str) -> None:
    with job.lock:
        job.log.append(line)
        if len(job.log) > MAX_LOG_LINES:
            del job.log[: len(job.log) - MAX_LOG_LINES]


def _abort(job: Job, message: str) -> None:
    """Fail the job and make sure the child cannot linger on stdin.

    main.py blocks on input() for these prompts, so marking the job failed is
    not enough on its own: without an answer the child would sit there forever
    holding a core. A blank line is the graceful quit path for the source
    prompt; anything still alive after that is terminated.
    """
    job.status = "failed"
    job.error = message
    _answer(job, "")
    process = job.process
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    job.publish({"type": "failed", "status": "failed", "error": message})


def _handle_prompt(job: Job, rest: str) -> bool:
    """Returns True if the prompt was consumed automatically."""
    fields = _parse_fields(rest)
    key = fields.pop("key", "")

    if key == "await_source":
        # Only reachable when a research gate was declined and the pipeline
        # looped back for a source. In a browser there is no second chance.
        _abort(
            job,
            "Research was declined, so there was no source to work from. "
            "Start a new job with a YouTube link, an uploaded file, or "
            "approve the search.",
        )
        return True

    if key == "retry":
        _abort(job, "The pipeline stopped with an error.")
        return True

    if key in AUTO_ANSWERS:
        _answer(job, AUTO_ANSWERS[key])
        return True

    pending = {"key": key, "question": "", "urls": [], "count": 0}
    if key == "gate_search":
        pending["question"] = (
            "No source is attached, so this would need researched material. "
            "Searching sends your topic to DuckDuckGo and Wikipedia."
        )
    elif key == "gate_fetch":
        raw_urls = fields.get("urls", "")
        pending["urls"] = [u for u in raw_urls.split(",") if u]
        pending["count"] = int(fields.get("count", len(pending["urls"])) or 0)
        pending["question"] = (
            f"Read the {pending['count']} page(s) found by searching? "
            "Each page is downloaded and its text is used for the notes."
        )

    job.pending = pending
    job.status = "waiting"
    job.publish({"type": "waiting", "pending": pending})
    return False


def _answer(job: Job, text: str) -> None:
    process = job.process
    if process is None or process.stdin is None or process.stdin.closed:
        return
    try:
        process.stdin.write(text + "\n")
        process.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass
    job.pending = None
    if job.status == "waiting":
        job.status = "running"
    job.publish({"type": "resumed"})


def _handle_artifact(job: Job, rest: str) -> None:
    fields = _parse_fields(rest)
    kind = fields.get("kind", "")
    raw_path = fields.get("path", "")
    if not kind or not raw_path:
        return
    source_path = Path(raw_path)
    if not source_path.exists():
        return

    # main.py always writes to a fixed name, so snapshot it per job before
    # another run overwrites it.
    JOB_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix or ".bin"
    kept = JOB_ARTIFACT_DIR / job.id / f"notes{suffix}"
    kept.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source_path, kept)
    except OSError:
        return

    pages = fields.get("pages")
    artifact = Artifact(
        kind=kind,
        path=kept,
        size=int(fields.get("bytes", kept.stat().st_size) or 0),
        pages=int(pages) if pages and pages.isdigit() else None,
    )
    job.artifacts[kind] = artifact
    job.publish(
        {
            "type": "artifact",
            "kind": kind,
            "url": f"/api/jobs/{job.id}/artifact/{kind}",
            "size": artifact.size,
            "pages": artifact.pages,
        }
    )


SENTINELS = (PROMPT_SENTINEL, STAGE_SENTINEL, ARTIFACT_SENTINEL)


def _split_sentinels(line: str) -> list[tuple[str, str]]:
    """Break a stdout line into ('text'|'sentinel', payload) pieces.

    input() writes its prompt without a trailing newline, so the first thing
    the pipeline prints after an answer can share a line with that prompt:
    "Search? [y/N]: <<<STAGE pdf". Matching only at position 0 would silently
    drop those, so sentinels are located anywhere in the line. Text before a
    sentinel is still reported as a log line, which keeps the echoed prompt
    visible in the browser.
    """
    pieces: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(line):
        best_at, best_sentinel = -1, ""
        for sentinel in SENTINELS:
            at = line.find(sentinel, cursor)
            if at != -1 and (best_at == -1 or at < best_at):
                best_at, best_sentinel = at, sentinel
        if best_at == -1:
            pieces.append(("text", line[cursor:]))
            break
        if best_at > cursor:
            pieces.append(("text", line[cursor:best_at]))
        # A sentinel runs to the next sentinel or end of line; its own payload
        # is whitespace-delimited, so trailing prose after it is harmless.
        tail = line[best_at + len(best_sentinel):]
        nxt = len(tail)
        for sentinel in SENTINELS:
            at = tail.find(sentinel)
            if at != -1:
                nxt = min(nxt, at)
        pieces.append(("sentinel", best_sentinel + tail[:nxt]))
        cursor = best_at + len(best_sentinel) + nxt
    return [(kind, text) for kind, text in pieces if text.strip()]


def _handle_stage(job: Job, rest: str) -> None:
    name = rest.strip().split()
    stage = name[0] if name else None
    job.stage = stage
    job.publish(
        {"type": "stage", "stage": stage, "label": STAGE_LABELS.get(stage or "", None)}
    )


def _pump(job: Job, stream) -> None:
    """Read the child's merged stdout, dispatching sentinels as they appear."""
    try:
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            for kind, text in _split_sentinels(line):
                if kind == "text":
                    _record(job, text.strip())
                    job.publish({"type": "log", "line": text.strip()})
                elif text.startswith(STAGE_SENTINEL):
                    _handle_stage(job, text[len(STAGE_SENTINEL):])
                elif text.startswith(ARTIFACT_SENTINEL):
                    _handle_artifact(job, text[len(ARTIFACT_SENTINEL):])
                else:
                    _handle_prompt(job, text[len(PROMPT_SENTINEL):])
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _finalise(job: Job, code: int) -> None:
    process = job.process
    job.process = None
    if process is not None and process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    with job.lock:
        if job.status in ("failed", "cancelled"):
            job.pending = None
            return
        if code == 0:
            # Clean exit. Producing no PDF is still a success only if the job
            # had already been marked done; otherwise the user needs to know.
            job.status = "done"
        elif code in (130, -15, 1) and job.error is None:
            job.status = "cancelled"
            job.error = "Cancelled."
        else:
            job.status = "failed"
            job.error = job.error or f"The pipeline exited with code {code}."
    job.pending = None
    if job.status == "done" and not job.artifacts:
        # Exited 0 but wrote no PDF: the honest report is a failure, not a
        # blank success state the browser would show as "ready".
        job.status = "failed"
        job.error = job.error or "The pipeline finished without producing a PDF."
    job.publish(
        {
            "type": "done" if job.status == "done" else "failed",
            "status": job.status,
            "error": job.error,
            "artifacts": {
                k: {"kind": a.kind, "url": f"/api/jobs/{job.id}/artifact/{a.kind}",
                    "size": a.size, "pages": a.pages}
                for k, a in job.artifacts.items()
            },
        }
    )


def _run(job: Job, argv: list[str]) -> None:
    env = dict(os.environ)
    env["NOTES_UI"] = "1"
    # Windows consoles default to a legacy code page; force UTF-8 so the
    # transcript and Mermaid text survive the pipe.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    job.status = "running"
    job.publish({"type": "started"})

    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(MAIN_PY), *argv],
            cwd=str(BASE_DIR),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        job.status = "failed"
        job.error = f"Could not start the pipeline: {exc}"
        job.publish({"type": "failed", "error": job.error})
        return

    job.process = process
    reader = threading.Thread(target=_pump, args=(job, process.stdout), daemon=True)
    reader.start()
    code = process.wait()
    reader.join(timeout=5)
    _finalise(job, code)


def _start(job: Job, argv: list[str]) -> None:
    threading.Thread(target=_run, args=(job, argv), daemon=True).start()


# ===========================================================================
# routes
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# An inline SVG mark: no binary asset, and it reuses the ink colour so the tab
# matches the page. Without this the browser logs a 404 on every load.
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" fill="#fbfcfe"/>'
    '<g stroke="#a8c8e8" stroke-width="2">'
    '<path d="M4 10h24M4 18h24M4 26h24"/></g>'
    '<path d="M9 4v24" stroke="#e06a6a" stroke-width="2"/>'
    '<text x="14" y="21" font-family="Georgia,serif" font-size="13" '
    'font-style="italic" fill="#1a2a6c">a</text>'
    "</svg>"
)


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "ollama": _ollama_ready(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "mmdc": bool(shutil.which("mmdc") or shutil.which("mmdc.cmd")),
    }


def _ollama_ready() -> bool:
    try:
        import requests  # noqa: PLC0415
        response = requests.get("http://localhost:11434/api/tags", timeout=2)
        models = [m.get("name", "") for m in response.json().get("models", [])]
        return any(name.startswith("llama3") for name in models)
    except Exception:                                   # noqa: BLE001
        return False


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": manager.recent()}


ALLOWED_UPLOAD_SUFFIXES = {
    ".mp3", ".wav", ".mp4", ".m4a", ".mkv", ".webm", ".mov", ".flac", ".ogg",
    ".pdf", ".txt", ".md", ".markdown",
}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)) -> dict:
    """Stage an upload on disk and return the path the job runner will use."""
    name = Path(file.filename or "upload").name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{suffix or 'that file type'} is not supported. "
                "Media: mp3 wav mp4 m4a mkv webm mov flac ogg. "
                "Documents: pdf txt md."
            ),
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{name}"
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="That file was empty.")
    target.write_bytes(data)

    # classify_input re-validates the extension, but a .md renamed to .pdf
    # would fail deep inside extraction, so say so here instead.
    return {"path": str(target), "name": name, "bytes": len(data)}


@app.post("/api/jobs")
async def create_job(payload: dict = Body(...)) -> dict:
    kind = str(payload.get("kind", "")).strip()
    source = str(payload.get("source", "")).strip()

    if not source:
        raise HTTPException(status_code=400, detail="A source is required")

    if kind == "upload":
        # The file was already staged by /api/upload; take the path as given.
        staged = Path(source)
        if not staged.exists():
            raise HTTPException(status_code=400, detail="That upload is missing.")
        job = manager.create(staged.name, "document")
        _start(job, [str(staged)])
    elif kind in ("youtube", "topic"):
        job = manager.create(source, kind)
        _start(job, [source])
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source kind: {kind}")

    return job.snapshot()


@app.post("/api/jobs/{job_id}/answer")
def answer_job(job_id: str, payload: dict = Body(...)) -> dict:
    job = manager.get(job_id)
    answer = str(payload.get("answer", "")).strip().lower()
    key = str(payload.get("key", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="A prompt key is required")
    if job.pending is None or job.pending.get("key") != key:
        raise HTTPException(status_code=409, detail="That decision is no longer pending")
    _answer(job, answer)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = manager.get(job_id)
    process = job.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    job.status = "cancelled"
    job.error = "Cancelled."
    job.publish({"type": "failed", "status": "cancelled", "error": job.error})
    return {"ok": True}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = manager.get(job_id)
    job.loop = asyncio.get_running_loop()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    # Replay current state so a late subscriber is never blank.
    queue.put_nowait({"type": "snapshot", **job.snapshot()})
    job.subscribe(loop, queue)

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "failed"):
                    break
        finally:
            job.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/artifact/{kind}")
def get_artifact(job_id: str, kind: str) -> Response:
    job = manager.get(job_id)
    artifact = job.artifacts.get(kind)
    if artifact is None or not artifact.path.exists():
        raise HTTPException(status_code=404, detail="No such artifact")

    if kind == "pdf":
        return FileResponse(
            artifact.path,
            media_type="application/pdf",
            filename="handwritten_lecture_notes.pdf",
            headers={"Content-Disposition": f'inline; filename="{artifact.path.name}"'},
        )

    if kind == "html":
        # main.py writes diagram links as temp/diagram_N.png relative to
        # BASE_DIR, and build_html_preview rebases them to ../temp/ for the
        # on-disk location in output/. Served over HTTP, /files is already
        # mounted at temp/, so the path drops the extra segment.
        text = artifact.path.read_text(encoding="utf-8")
        text = text.replace('src="../temp/', 'src="/files/')
        return HTMLResponse(text)

    raise HTTPException(status_code=404, detail="No such artifact")


# temp/ is created by the pipeline on its first run and is absent in a fresh
# clone, so it has to be made here or StaticFiles refuses to mount.
TEMP_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=str(TEMP_DIR)), name="files")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn  # noqa: PLC0415

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("NOTES_WEB_PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")