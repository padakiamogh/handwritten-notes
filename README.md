# Handwritten PDF Study Notes Generator

Turns a lecture, article, or topic into **handwritten-style PDF study notes** on
ruled notebook paper, with **Mermaid diagrams rendered as real images**.

Supported sources:

| Input | How it is read |
|---|---|
| YouTube video / playlist URL | `yt-dlp` downloads audio -> Whisper transcribes |
| Local audio / video | Whisper transcribes directly |
| Local `.pdf` | `pypdf` extracts text per page |
| Local `.txt` / `.md` | read directly |
| A bare topic | **asks first**, then reads web sources |

---

## The PDF it produces

- Handwritten font (Kalam, auto-downloaded) in classic blue fountain-pen ink `#1a2a6c`
- Light blue horizontal ruling every **24pt**, matching the 24pt line leading
- A red vertical margin line on the left
- Markdown headings, bullet and numbered lists, tables, block quotes, code blocks
- Mermaid diagrams rendered to PNG and embedded
- A **Sources** section listing every web page actually read, with clickable links

---

## Installation

### 1. Python dependencies

```bash
pip install -r requirements.txt
```

or explicitly:

```bash
pip install yt-dlp openai-whisper pypdf reportlab markdown ollama requests trafilatura
```

> **Note:** this project needs **reportlab 5.x or newer**. In reportlab 5,
> `Preformatted` / `XPreformatted` take a `ParagraphStyle` instead of the old
> `fontName` / `fontSize` keyword arguments.

### 2. FFmpeg (required for Whisper and yt-dlp)

```bash
winget install Gyan.FFmpeg          # Windows
brew install ffmpeg                # macOS
sudo apt install ffmpeg            # Debian / Ubuntu
```

### 3. Mermaid CLI (renders diagrams to PNG)

```bash
npm install -g @mermaid-js/mermaid-cli
```

This pulls Puppeteer, which downloads a Chromium build on first use. On Windows
the executable is a `mmdc.cmd` shim, which the script resolves automatically.

### 4. Ollama + a local model

```bash
# install from https://ollama.com/download
ollama serve                       # if it is not already running
ollama pull llama3
```

Confirm with `ollama list`. The model name is set in `main.py` via
`LLM_MODEL = "llama3"`.

---

## Usage

Interactive (recommended):

```bash
python main.py
```

```
==============================================================
  Enter a source
==============================================================
  - YouTube URL or playlist link
  - Local file: .mp3 .wav .mp4 .m4a .mkv .webm .mov .flac .ogg
           or .pdf .txt .md
  - A bare topic, which will ask before searching the web

  Source (blank to quit):
```

Or pass the source directly:

```bash
python main.py "https://www.youtube.com/watch?v=VIDEO_ID"
python main.py "C:\lectures\lecture01.mp4"
python main.py "C:\notes\chapter3.pdf"
python main.py "the light reactions of photosynthesis"
```

A command-line topic still asks for permission before searching. The CLI never
bypasses the consent prompts.

Output: `output/handwritten_lecture_notes.pdf`
and `output/handwritten_lecture_notes.html` (the same notes, readable in a browser)

---

## Web front end (optional)

`main.py` is still the engine. `webui/server.py` is a thin supervisor around it
that gives you a browser interface, a live progress view, and an on-screen read
of the finished notes.

```bash
pip install fastapi "uvicorn[standard]" python-multipart
python webui/server.py
```

Then open <http://127.0.0.1:8000>.

To use a different port:

```bash
python webui/server.py                    # 127.0.0.1:8000 by default
$env:NOTES_WEB_PORT = 8080; python webui/server.py     # PowerShell
NOTES_WEB_PORT=8080 python webui/server.py             # bash
```

### Why it is built this way

The pipeline is CPU-bound and slow. llama3 inference alone takes minutes, longer
on a machine with no GPU. So nothing runs inside a request handler. Each run is
a **job**: `main.py` is spawned as a subprocess, its output is parsed, and
progress is streamed to the browser over Server-Sent Events. A job is snapshotted
per run under `output/jobs/<id>/`, because `main.py` always writes to a fixed
filename and a second run would otherwise overwrite the first.

`main.py` talks to the supervisor through three sentinels, printed only when the
`NOTES_UI` environment variable is set. Without it the CLI output is unchanged.

| Sentinel | Meaning |
|---|---|
| `<<<PROMPT key=...` | a decision is now blocking the pipeline |
| `<<<STAGE name` | the pipeline moved to a new phase |
| `<<<ARTIFACT kind= path= ...` | a PDF or HTML file now exists |

### Consent still applies in the browser

The two research gates are **not** auto-answered. When `main.py` reaches a gate
it genuinely blocks on `input()`; the browser is told a decision is pending and
posts your answer back into the subprocess. If you decline a search there is no
source to work from, so the job stops and says so rather than carrying on.

### Layout

- **Left rail**: pick a source (YouTube / topic / file upload), start a job, see
  recent jobs. The footer reports whether Ollama, FFmpeg and `mmdc` are present.
- **The page**: a progress spine over live output, the consent gate when one is
  pending, and the PDF plus a browser preview when the job finishes.

It is styled from the same six colours the PDF already uses (paper, rule, ink,
margin, graphite, pencil) on the same 24pt grid, so the app and its output read
as one object.

---

## Web research is consent-gated

The tool only searches the web when you give it a **bare topic with no source
attached**, and it asks **twice**:

```
Gate 1   "Search the web for 'the water cycle'? [y/N]"
              |
              +-- No --> asks you for a URL or file path instead
              |
              +-- Yes --> searches Wikipedia + DuckDuckGo
                              |
                              v
Gate 2   shows the actual URLs found
         "Which pages should I read? all / none / 1,3,5"
                              |
                              +-- none --> asks you for a source instead
                              |
                              +-- approved --> downloads and reads only those
```

Both gates **default to No**. Declining either one sends you back to the input
prompt to supply a source. Notes are never generated from unverified model
memory.

No source means no web access: if you attach a YouTube link or a file, the tool
processes it and stops. There is no way to combine an attached source with web
research.

### Sources used

Both are **keyless** - no account, no API key:

- **Wikipedia API** - queried first; its text is already retrieved, so those
  entries need no download and always appear as `[already retrieved]`
- **DuckDuckGo HTML endpoint** - fills the remaining slots

Many sites (Britannica, sciencenotes, and others) block automated downloads
with HTTP 403. The script reports each skip with its status and continues;
Wikipedia guarantees at least one usable source for almost any topic.

---

## How it works

```
source ──> extract text ──> map-reduce note generation ──> mermaid ──> PDF
           (yt-dlp/          (llama3, chunked +            (mmdc)     (reportlab,
            whisper/           consolidated)                            notebook
            pypdf)                                                    canvas)
```

### Long inputs are chunked

`llama3` has an ~8k token context, and a one-hour video transcript runs to
~50k characters. A single request would be silently truncated or rejected, so
the text is split at paragraph then sentence boundaries (no sentence is ever
lost) and processed as a **map-reduce**:

1. **Map** - detailed notes drafted per chunk, each told which section it is so
   it does not restate earlier material
2. **Reduce** - a consolidation pass merges the drafts, removes duplication,
   renumbers, and preserves every Mermaid block verbatim

### Fonts

`assets/fonts/Kalam-Regular.ttf` is downloaded on first run.

> Caveat is only published as a **variable-weight** TTF in the google/fonts
> repository, which ReportLab cannot register. Kalam is a static font and is
> used instead. The spec allowed either.

To use a different font, drop any static `.ttf` into `assets/fonts/` and update
the candidates list in `ensure_handwritten_font()`. If no font can be loaded,
the script falls back to Helvetica and says so.

### The notebook ruling

Two details make the ruling come out right, and both are easy to get backwards:

**The ruling is prepended to the content stream, not painted at page start.** When
a page closes, its ruling operators are inserted at the *front* of that page's
content stream. Prepended operators are emitted first, so they paint underneath
everything drawn afterwards. Painting them at page start would put an unused
trailing page's worth of ruling into the stream - and since ReportLab's
`Canvas.save()` only flushes a page when `len(self._code)` is non-zero, that
would append a blank final sheet to every document.

**Every page gets ruled, not just the first.** Drawing the ruling only in
`__init__` covers page 1; pages 2..N come out blank. `showPage()` adds the ruling
to the page being closed and guards against double-drawing page 1.

The verified result is 29 rules per page at exactly 24.0pt spacing, one red
margin line at x=72, and no trailing blank page.

---

## Project layout

```
handwritten-notes/
├── main.py                  # the whole pipeline
├── requirements.txt
├── README.md
├── assets/fonts/            # auto-downloaded TTF
├── temp/                    # working files (gitignored)
│   ├── *.mp3                # downloaded audio
│   ├── *.transcript.txt     # Whisper output
│   ├── diagram_N.mmd/.png   # Mermaid sources + renders
│   ├── uploads/             # files uploaded through the web front end
│   └── web/                 # cached pages and API responses
├── webui/                   # optional browser front end
│   ├── server.py            # job supervisor + API (spawns main.py)
│   └── static/              # index.html, style.css, app.js
└── output/
    ├── handwritten_lecture_notes.pdf
    ├── handwritten_lecture_notes.html
    └── jobs/<id>/notes.pdf  # per-job snapshot, so runs do not overwrite
```

`temp/web/` caches fetched pages and API responses for 24 hours, so repeat runs
on the same topic are fast and do not re-hit Wikipedia.

---

## Troubleshooting

**`mermaid-cli not found`**
`npm install -g @mermaid-js/mermaid-cli`. On Windows the script looks for
`mmdc.cmd` specifically, because `mmdc` is a PowerShell `.ps1` shim that
`subprocess` cannot execute directly.

**`error: unknown option '-w'`**
You have an old mermaid-cli. The script uses `--size`; update with
`npm install -g @mermaid-js/mermaid-cli@latest`.

**`This video is not available`**
yt-dlp can list a video in search results but still refuse to download it. This
happens with age-gated, region-locked, and some embedding-restricted videos, and
YouTube also throttles or blocks downloads from some IP ranges. Try a different
video, or attach the audio file directly (`python main.py lecture.mp3`) - the
transcription and notes stages are identical either way. A failed download is
reported and you are returned to the source prompt; it never aborts the run.

**Whisper is slow**
The `base` model on CPU transcribes at roughly real time or slower - a 1-hour
lecture can take a while. Set `WHISPER_MODEL` in `main.py` to `"small"` for
better accuracy or `"tiny"` for speed, or run with a CUDA build of PyTorch.

**"most pages yielded little text"**
The PDF is scanned images with no OCR layer. This tool has no OCR; run the file
through an OCR tool first.

**Diagram could not be rendered**
The Mermaid source is preserved in the PDF as a code block instead, so the
document still builds. LLMs most often emit `-->|label|>` with a stray `>` after
the edge label, or omit the diagram header; both are repaired automatically, and
a second attempt drops the edge labels entirely.

**Ollama not reachable / model missing**
Start the server with `ollama serve`, then `ollama pull llama3`.

**Notes seem truncated**
They should not be - chunking is automatic. If output is thin, the source text
itself was probably thin (for example a failed transcription).

**A web job sits at "waiting for your decision"**
That is a consent gate with nobody answering it - open the tab and choose Allow
or Decline. Answering is what resumes the job; there is no timeout. To abandon
it instead, use Cancel.

**A web job says "The pipeline finished without producing a PDF"**
`main.py` exited cleanly but wrote no file. The real reason is in the job's log
on the page, just above this message.

**Nothing happens in the browser after clicking Begin**
Check the footer in the left rail. It reports whether Ollama, FFmpeg and `mmdc`
were found. A long silence on "Writing notes" is normal on a CPU-only machine -
llama3 with no GPU can take several minutes per chunk.

---

## Notes and limits

- `.mkdir` in the original spec is treated as a typo for `.m4a`; the accepted
  media set is `.mp3 .wav .mp4 .m4a .mkv .webm .mov .flac .ogg`
- A path that does not exist is treated as a topic, with a printed notice. The
  Gate 1 prompt is the safeguard against a typo causing an unwanted search
- Web research scrapes search results, so it can break if a site changes its
  markup. It degrades to fewer sources rather than failing
