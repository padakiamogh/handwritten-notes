#!/usr/bin/env python3
"""
Handwritten PDF Study Notes Generator
=====================================

Turns a source into handwritten-style study notes with rendered Mermaid diagrams.

Source types
    * YouTube video or playlist URL  -> yt-dlp -> Whisper transcription
    * Local audio / video file       -> Whisper transcription
    * Local PDF / TXT / MD file      -> direct text extraction
    * A bare topic                   -> CONSENT-GATED web research (see below)

Consent model
    The tool only touches the web when you give it a bare topic with no source
    attached, and it asks twice first:

        Gate 1  "Search the web for '<topic>'?"        (defaults to No)
        Gate 2  "Read these N pages?" + URL listing    (defaults to No)

    Declining either gate sends you back to the input prompt to supply a source.
    Notes are never generated from unverified model memory.

Output
    output/handwritten_lecture_notes.pdf  -- blue ruled notebook pages,
    red left margin line, blue fountain-pen ink, embedded Mermaid diagrams.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

# ---------------------------------------------------------------------------
# Optional third-party imports. Missing ones degrade gracefully with a clear
# message rather than an ImportError traceback.
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    requests = None

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    import markdown as markdown_lib
except ImportError:
    markdown_lib = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import ollama
except ImportError:
    ollama = None

import whisper  # noqa: E402  (imported late so --help works without it)
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)

# ===========================================================================
# 1. CONFIGURATION
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
FONT_DIR = ASSETS_DIR / "fonts"
TEMP_DIR = BASE_DIR / "temp"
WEB_CACHE_DIR = TEMP_DIR / "web"
OUTPUT_DIR = BASE_DIR / "output"

# --- models ---
LLM_MODEL = "llama3"
WHISPER_MODEL = "base"

# --- notebook styling (spec) ---
FONT_SIZE = 16
LEADING = 24            # must match notebook ruling spacing
RULE_GAP = 24           # vertical distance between ruling lines
INK_COLOR = "#1a2a6c"   # classic blue fountain pen ink
RULE_COLOR = "#a8c8e8"  # light blue ruling
MARGIN_RED = "#e06a6a"  # red margin line

FONT_NAME = "HandwrittenFont"
FALLBACK_FONT = "Helvetica"
CODE_FONT = "Courier"

# --- page geometry ---
PAGE_LEFT = 90
PAGE_RIGHT = 54
PAGE_TOP = 54
PAGE_BOTTOM = 54
MARGIN_LINE_X = 72      # red line sits 18pt left of the text column
RULE_OFFSET = 8         # nudges the ruling so baselines sit on the lines
FRAME_PADDING = 6       # ReportLab Frame default padding on each side

# --- fonts ---
KALAM_URL = "https://github.com/google/fonts/raw/main/ofl/kalam/Kalam-Regular.ttf"
CAVEAT_URL = (
    "https://github.com/google/fonts/raw/main/ofl/caveat/Caveat%5Bwght%5D.ttf"
)

# --- research ---
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia's policy asks for a descriptive agent, not a fake browser string.
WIKI_UA = "handwritten-notes/1.0 (personal study-notes generator, local use)"
CACHE_MAX_AGE = 24 * 3600        # seconds
MAX_SOURCES = 5
MAX_SEARCH_RESULTS = 8
MIN_SOURCE_CHARS = 500   # below this a page is a paywall / JS shell / 404
FETCH_TIMEOUT = 20
POLITE_DELAY = 0.5

# --- llm chunking ---
CHUNK_CHARS = 6000
LLM_OPTIONS = {"temperature": 0.3, "num_ctx": 8192}

PDF_NAME = "handwritten_lecture_notes.pdf"
HTML_NAME = "handwritten_lecture_notes.html"

MEDIA_EXTS = {
    ".mp3", ".wav", ".mp4", ".m4a", ".mkv",
    ".webm", ".mov", ".flac", ".ogg",
}
DOCUMENT_EXTS = {".pdf", ".txt", ".md", ".markdown"}
YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}

MERMAID_KEYWORDS = (
    "flowchart", "graph", "sequencediagram", "classdiagram", "statediagram",
    "statediagram-v2", "erdiagram", "journey", "gantt", "pie", "mindmap",
    "timeline", "quadrantchart", "gitgraph", "c4context", "requirementdiagram",
    "sankey-beta",
)


# ===========================================================================
# 2. HELPERS
# ===========================================================================

def ensure_dirs() -> None:
    for directory in (FONT_DIR, TEMP_DIR, WEB_CACHE_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def safe_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return (slug[:max_len] or "source").lower()


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def die(message: str, code: int = 1) -> None:
    print(f"\nError: {message}", file=sys.stderr)
    raise SystemExit(code)


def require(module, name: str, package: str) -> None:
    if module is None:
        die(f"'{name}' is not installed. Run:  pip install {package}")


# --- UI bridge -------------------------------------------------------------
# The web front end drives this script as a subprocess and has to render the
# consent gates as real browser decisions rather than auto-answering them. When
# NOTES_UI is set, every blocking prompt emits a sentinel line first so the
# parent process knows a decision is pending. Unset (the CLI) prints nothing
# extra, so terminal behaviour is unchanged.
PROMPT_SENTINEL = "<<<PROMPT"
STAGE_SENTINEL = "<<<STAGE"
ARTIFACT_SENTINEL = "<<<ARTIFACT"
UI_MODE = bool(os.environ.get("NOTES_UI"))


def announce_prompt(key: str, extra: str = "") -> None:
    """Tell a supervising UI that a decision is now blocking the pipeline."""
    if not UI_MODE:
        return
    print(f"{PROMPT_SENTINEL} {key}{extra}", flush=True)


def prompt_yes_no(question: str, default: bool = False,
                  key: str = "") -> bool:
    """Consent primitive. Always defaults to the safe answer (No).

    Re-prompts on unrecognised input. EOF / Ctrl-C declines rather than
    crashing, so a piped or closed stdin can never silently authorise anything.
    """
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            if key:
                announce_prompt(key)
            answer = input(f"{question} {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer 'y' or 'n'.")


def banner(text: str) -> None:
    print(f"\n{'=' * 62}\n  {text}\n{'=' * 62}")


def announce_stage(name: str) -> None:
    """Name the pipeline phase a supervising UI should mark as in progress."""
    if not UI_MODE:
        return
    print(f"{STAGE_SENTINEL} {name}", flush=True)


def announce_artifact(kind: str, path: Path, **extra) -> None:
    """Tell a supervising UI a deliverable now exists on disk."""
    if not UI_MODE:
        return
    fields = " ".join(f"{k}={v}" for k, v in extra.items())
    print(
        f"{ARTIFACT_SENTINEL} kind={kind} path={path} {fields}".rstrip(),
        flush=True,
    )


# ===========================================================================
# 3. HANDWRITTEN FONT
# ===========================================================================

def ensure_handwritten_font() -> str:
    """Download + register the handwritten TTF. Falls back to Helvetica."""
    candidates = [
        ("Kalam-Regular.ttf", KALAM_URL),
        ("Caveat-Regular.ttf", CAVEAT_URL),
    ]
    local_path = None

    for filename, url in candidates:
        target = FONT_DIR / filename
        if target.exists() and target.stat().st_size > 10_000:
            local_path = target
            break
        if requests is None:
            continue
        try:
            print(f"  Fetching handwritten font {filename} ...")
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            if len(response.content) < 10_000:
                continue
            target.write_bytes(response.content)
            local_path = target
            break
        except Exception as exc:                       # noqa: BLE001
            print(f"  Could not fetch {filename}: {exc}")

    if local_path is not None:
        try:
            pdfmetrics.registerFont(TTFont(FONT_NAME, str(local_path)))
            # Lets <b>/<i> resolve instead of falling back to a family error.
            pdfmetrics.registerFontFamily(
                FONT_NAME, normal=FONT_NAME, bold=FONT_NAME,
                italic=FONT_NAME, boldItalic=FONT_NAME,
            )
            print(f"  Using handwritten font: {local_path.name}")
            return FONT_NAME
        except Exception as exc:                       # noqa: BLE001
            print(f"  {local_path.name} could not be registered: {exc}")

    print("  WARNING: handwritten font unavailable, falling back to Helvetica.")
    return FALLBACK_FONT


# ===========================================================================
# 4. RESEARCH MODULE  (consent-gated)
# ===========================================================================

def consent_to_search(topic: str) -> bool:
    """Gate 1 - permission to run any network search."""
    banner("Web research - permission to search")
    print(f"  Topic : {topic}")
    print("  No source file is attached, so notes would need researched material.")
    print("  This sends your topic to DuckDuckGo and Wikipedia.")
    return prompt_yes_no("\n  Search the web for sources now?", default=False,
                         key="gate_search")


def consent_to_fetch(candidates: list[dict]) -> list[dict]:
    """Gate 2 - show the actual URLs, then ask before downloading any page."""
    banner("Web research - permission to read pages")
    print("  Search returned these pages:\n")
    for index, entry in enumerate(candidates, start=1):
        title = entry.get("title") or "(untitled)"
        note = "  [already retrieved, no download]" if entry.get("prefetched") else ""
        print(f"    {index:>2}. {title[:68]}{note}")
        print(f"        {entry['url']}")

    print("\n  Choose: 'all', 'none', or specific numbers (e.g. 1,3,5)")
    while True:
        try:
            # A UI needs the candidate list, not just a yes/no, so the choices
            # are serialised into the sentinel line.
            if UI_MODE:
                announce_prompt(
                    "gate_fetch",
                    " count=" + str(len(candidates))
                    + " urls=" + ",".join(entry["url"] for entry in candidates[:8]),
                )
            answer = input("\n  Which pages should I read? [none]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if not answer or answer in ("none", "n", "no"):
            return []
        if answer in ("all", "a"):
            return candidates
        numbers = re.findall(r"\d+", answer)
        if numbers:
            chosen = [
                candidates[int(n) - 1]
                for n in numbers
                if n.isdigit() and 1 <= int(n) <= len(candidates)
            ]
            if chosen:
                return chosen
        print(f"  Enter 'all', 'none', or numbers between 1 and {len(candidates)}.")


def _clean_title(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw_html)
    return html.unescape(text).strip()


def _get_with_retry(url: str, params: dict | None = None,
                    headers: dict | None = None, tries: int = 3):
    """GET with backoff. Wikimedia answers 429 when a client hammers it."""
    require(requests, "requests", "requests")
    last_error = "request failed"
    for attempt in range(tries):
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=FETCH_TIMEOUT
            )
            if response.status_code == 429:
                last_error = "HTTP 429 (rate limited)"
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 429:
                last_error = "HTTP 429 (rate limited)"
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(last_error)


def _cached_json(url: str, params: dict, cache_key: str, headers: dict) -> dict:
    """Fetch JSON, memoised on disk so repeat runs never re-hit the API."""
    cache_file = WEB_CACHE_DIR / f"{hashlib.sha1(cache_key.encode()).hexdigest()[:16]}.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < CACHE_MAX_AGE:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass                            # corrupt cache: fall through and refetch
    data = _get_with_retry(url, params=params, headers=headers).json()
    try:
        cache_file.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def _relevance(title: str, topic: str) -> int:
    """Rank wiki hits so the canonical article beats 'Artificial <topic>'."""
    lowered, query = title.lower(), topic.lower().strip()
    score = 0
    if lowered == query:
        score += 100
    elif lowered.startswith(query):
        score += 60
    if query in lowered:
        score += 30
    for word in re.findall(r"[a-z0-9]+", query):
        if len(word) > 3 and word in lowered:
            score += 10
    if "disambiguation" in lowered:
        score -= 60
    return score


def _normalise_result_url(url: str) -> str:
    """DuckDuckGo sometimes wraps results in a /l/?uddg= redirect."""
    if url.startswith("//"):
        url = "https:" + url
    if "duckduckgo.com/l/" in url:
        match = re.search(r"uddg=([^&]+)", url)
        if match:
            return urllib.parse.unquote(match.group(1))
    return url


def search_web(query: str, max_results: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """Keyless DuckDuckGo HTML search. Returns [] instead of raising."""
    require(requests, "requests", "requests")
    results: list[dict] = []
    try:
        response = requests.post(
            DDG_ENDPOINT, data={"q": query}, headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT,
        )
        response.raise_for_status()
        pattern = r'class="result__a"\s+href="([^"]+)"[^>]*>(.*?)</a>'
        for url, raw_title in re.findall(pattern, response.text, re.S):
            clean_url = _normalise_result_url(html.unescape(url))
            if not clean_url.startswith("http"):
                continue
            results.append({"title": _clean_title(raw_title), "url": clean_url})
            if len(results) >= max_results:
                break
    except Exception as exc:                           # noqa: BLE001
        print(f"  DuckDuckGo search unavailable ({exc}); Wikipedia only.")
    return results


def wikipedia_lookup(topic: str, max_pages: int = 3) -> list[dict]:
    """Keyless Wikipedia search + full plain-text extracts."""
    require(requests, "requests", "requests")
    pages: list[dict] = []
    try:
        search_data = _cached_json(
            WIKI_API,
            {"action": "query", "list": "search", "srsearch": topic,
             "format": "json", "srlimit": max(max_pages + 2, 5)},
            f"wiki-search::{topic}",
            {"User-Agent": WIKI_UA},
        )
        hits = search_data.get("query", {}).get("search", [])
        # Put the canonical article first.
        hits.sort(key=lambda hit: _relevance(hit["title"], topic), reverse=True)
        titles = [hit["title"] for hit in hits[:max_pages]]
        if not titles:
            return pages

        extract_data = _cached_json(
            WIKI_API,
            {"action": "query", "prop": "extracts", "explaintext": 1,
             "titles": "|".join(titles), "format": "json", "redirects": 1},
            "wiki-extract::" + "|".join(titles),
            {"User-Agent": WIKI_UA},
        )
        for page in extract_data.get("query", {}).get("pages", {}).values():
            text = (page.get("extract") or "").strip()
            if len(text) < MIN_SOURCE_CHARS:
                continue
            title = page.get("title", "Untitled")
            pages.append({
                "title": title,
                "url": f"https://en.wikipedia.org/wiki/"
                       f"{urllib.parse.quote(title.replace(' ', '_'))}",
                "text": text,
            })
    except Exception as exc:                           # noqa: BLE001
        print(f"  Wikipedia lookup failed: {exc}")
    return pages


def fetch_and_extract(urls: list[dict]) -> list[dict]:
    """Download approved pages and strip them to readable text."""
    require(requests, "requests", "requests")
    if trafilatura is None:
        die("'trafilatura' is not installed. Run:  pip install trafilatura")

    fetched: list[dict] = []
    for position, entry in enumerate(urls):
        url = entry["url"]
        if position:
            time.sleep(POLITE_DELAY)

        cache_file = WEB_CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.html"
        html_text = ""
        try:
            if cache_file.exists():
                html_text = cache_file.read_text(encoding="utf-8", errors="ignore")
                print(f"  [{position + 1}/{len(urls)}] cached  {url[:56]}")
            else:
                print(f"  [{position + 1}/{len(urls)}] fetching {url[:56]}")
                response = _get_with_retry(
                    url, headers={"User-Agent": USER_AGENT}, tries=2
                )
                html_text = response.text
                cache_file.write_text(html_text, encoding="utf-8", errors="ignore")
        except Exception as exc:                       # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"HTTP {status}" if status else type(exc).__name__
            print(f"      skipped ({reason})")
            continue

        try:
            text = trafilatura.extract(html_text) or ""
        except Exception:                              # noqa: BLE001
            text = ""

        if len(text.strip()) < MIN_SOURCE_CHARS:
            print("      skipped (paywall, JS-only page, or too little text)")
            continue

        fetched.append({
            "title": entry.get("title") or urllib.parse.urlparse(url).netloc,
            "url": url,
            "text": text.strip(),
        })
    return fetched


def gather_sources(topic: str) -> tuple[str, list[dict]] | None:
    """Full consent-gated research flow.

    Returns (annotated_text_blob, citations) or None if the user declined at
    either gate, in which case the caller should ask for a source instead.
    """
    if not consent_to_search(topic):
        return None

    banner("Searching")
    print("  Querying Wikipedia (highest-signal study source) ...")
    wiki_pages = wikipedia_lookup(topic)
    print(f"    Wikipedia: {len(wiki_pages)} article(s), "
          f"{sum(len(p['text']) for p in wiki_pages):,} chars retrieved")

    print("  Querying DuckDuckGo for additional sources ...")
    web_hits = search_web(topic)
    print(f"    DuckDuckGo: {len(web_hits)} result(s)")

    # Wikipedia text is already in hand, so those entries cost nothing to use
    # and never need re-downloading. Web hits still have to be fetched, and
    # many sites hard-block scrapers, so Wikipedia is what guarantees that a
    # topic always yields at least one usable source.
    candidates: list[dict] = []
    seen_domains: set[str] = set()
    for page in wiki_pages:
        candidates.append({**page, "prefetched": True})
        seen_domains.add(urllib.parse.urlparse(page["url"]).netloc.lower())

    for hit in web_hits:
        domain = urllib.parse.urlparse(hit["url"]).netloc.lower()
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        candidates.append({**hit, "prefetched": False})
        if len(candidates) >= MAX_SOURCES:
            break

    if not candidates:
        print("\n  The web search returned nothing usable.")
        return None

    approved = consent_to_fetch(candidates)
    if not approved:
        print("\n  No pages approved - skipping web research.")
        return None

    to_download = [entry for entry in approved if not entry.get("prefetched")]
    downloaded = fetch_and_extract(to_download) if to_download else []

    # Preserve the order the user approved, interleaving pre-fetched text.
    by_url = {entry["url"]: entry for entry in downloaded}
    sources = [
        {"title": entry.get("title"), "url": entry["url"],
         "text": entry.get("text") or by_url.get(entry["url"], {}).get("text", "")}
        for entry in approved
    ]
    sources = [source for source in sources if len(source["text"].strip()) >= MIN_SOURCE_CHARS]

    if not sources:
        print("\n  None of the approved pages yielded readable text.")
        print("  (Some sites block automated downloads; Wikipedia is the")
        print("   most reliable source.)")
        return None

    banner("Sources gathered")
    for index, source in enumerate(sources, start=1):
        print(f"  [{index}] {source['title'][:60]}  ({len(source['text'])} chars)")

    chunks = [
        f"[SOURCE {index}: {source['title']} - {source['url']}]\n{source['text']}"
        for index, source in enumerate(sources, start=1)
    ]
    citations = [
        {"n": index, "title": source["title"], "url": source["url"]}
        for index, source in enumerate(sources, start=1)
    ]
    return "\n\n" + ("\n\n" + "=" * 60 + "\n\n").join(chunks), citations


# ===========================================================================
# 5. INPUT CLASSIFICATION
# ===========================================================================

def classify_input(raw: str) -> tuple[str, object]:
    """-> ('youtube'|'media'|'document'|'topic', payload)"""
    text = raw.strip().strip('"').strip("'")

    if re.match(r"^https?://", text, re.I):
        host = urllib.parse.urlparse(text).netloc.lower()
        if host in YOUTUBE_HOSTS:
            return "youtube", text
        return "topic", text          # non-YouTube URL: research the page's topic

    path = Path(text).expanduser()
    if path.exists() and path.is_file():
        suffix = path.suffix.lower()
        if suffix in MEDIA_EXTS:
            return "media", path
        if suffix in DOCUMENT_EXTS:
            return "document", path

    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith(("\\\\", "/")):
        # Clearly meant to be a path but does not exist.
        print(f"  Note: no file found at '{text}'. Treating it as a topic.")

    return "topic", text


# ===========================================================================
# 6. SOURCE EXTRACTORS
# ===========================================================================

def download_youtube_audio(url: str) -> list[Path]:
    """yt-dlp audio extraction. Returns every downloaded path (playlists > 1)."""
    import yt_dlp

    output_template = str(TEMP_DIR / "%(title).150B.%(ext)s")
    options = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "192"}
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "retries": 3,
    }
    before = set(TEMP_DIR.glob("*.mp3"))
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries", []) if e]
            print(f"  Playlist with {len(entries)} item(s).")
        else:
            entries = [info]

        downloaded: list[Path] = []
        for entry in entries:
            target = Path(downloader.prepare_filename(entry)).with_suffix(".mp3")
            if target.exists():
                downloaded.append(target)          # already fetched earlier
                continue
            if downloader.download([entry.get("url", url)]):
                if target.exists():
                    downloaded.append(target)
                else:
                    # Fall back to a scan restricted to what this call added.
                    downloaded.extend(sorted(set(TEMP_DIR.glob("*.mp3")) - before))
                    before = set(TEMP_DIR.glob("*.mp3"))
    if not downloaded:
        raise RuntimeError("yt-dlp produced no audio files")
    return downloaded


def transcribe_media(paths: list[Path]) -> str:
    print(f"  Loading Whisper model '{WHISPER_MODEL}' (first run downloads it) ...")
    model = whisper.load_model(WHISPER_MODEL)

    sections: list[str] = []
    for index, path in enumerate(paths, start=1):
        label = f"Item {index}" if len(paths) > 1 else path.stem
        print(f"  Transcribing {label} ({human_size(path.stat().st_size)}) ...")
        # fp16=False keeps this safe on CPU-only machines.
        result = model.transcribe(str(path), fp16=False)
        text = (result.get("text") or "").strip()
        transcript_file = TEMP_DIR / f"{safe_slug(path.stem)}.transcript.txt"
        transcript_file.write_text(text, encoding="utf-8")
        print(f"    -> {len(text)} chars -> {transcript_file.name}")
        sections.append(f"[{label}]\n{text}")

    return "\n\n".join(section for section in sections if section.strip())


def extract_pdf_text(path: Path) -> str:
    require(PdfReader, "pypdf", "pypdf")
    reader = PdfReader(str(path))
    parts: list[str] = []
    sparse_pages = 0
    for number, page in enumerate(reader.pages, start=1):
        try:
            page_text = (page.extract_text() or "").strip()
        except Exception:                              # noqa: BLE001
            page_text = ""
        if len(page_text) < 50:
            sparse_pages += 1
        parts.append(f"--- Page {number} ---\n{page_text}")

    if sparse_pages > len(reader.pages) / 2:
        print("  WARNING: most pages yielded little text. This PDF is probably")
        print("           scanned - no OCR layer is available, so notes will be thin.")
    return "\n\n".join(parts)


def extract_txt_text(path: Path) -> str:
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


# ===========================================================================
# 7. OLLAMA CLIENT
# ===========================================================================

def ensure_llm_ready() -> None:
    require(ollama, "ollama", "ollama")
    try:
        names = {model.get("model", "") for model in ollama.list().get("models", [])}
    except Exception as exc:                           # noqa: BLE001
        die(f"cannot reach Ollama at {exc}. Is 'ollama serve' running?")
    if not any(name.split(":")[0] == LLM_MODEL for name in names):
        die(f"model '{LLM_MODEL}' not pulled. Run:  ollama pull {LLM_MODEL}")


def chat(system_prompt: str, user_prompt: str) -> str:
    """One streamed completion from the local model."""
    stream = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options=LLM_OPTIONS,
        stream=True,
    )
    parts: list[str] = []
    for chunk in stream:
        parts.append(chunk.message.content or "")
        print(".", end="", flush=True)
    print()
    return "".join(parts).strip()


# ===========================================================================
# 8. NOTE GENERATION  (map-reduce)
# ===========================================================================

SYSTEM_PROMPT = """\
You are an expert tutor writing handwritten study notes for a student.

Rules:
1. Write COMPLETE, DETAILED notes. Never summarise or abbreviate. Cover every \
concept, every step, and every example found in the source material.
2. Explain the 'why', not just the 'what'. Define terms. State prerequisites.
3. Use Markdown headings (#, ##, ###) and bullet lists so the notes are scannable.
4. IMPORTANT: insert a Mermaid diagram for ANY workflow, process, pipeline, \
architecture, state machine, or side-by-side comparison. Wrap each one in a \
fenced block starting with the word mermaid, for example:

```mermaid
flowchart TD
    A[Input] --> B[Transform]
    B --> C[Output]
```

5. Use valid Mermaid syntax: quote any label containing parentheses or \
brackets, and always start the diagram with a diagram keyword such as flowchart.
6. Preserve any concrete numbers, names, code, or examples from the source.
7. Output only the notes themselves. No preamble, no closing remarks.
"""

CONSOLIDATION_PROMPT = """\
Below are draft study-note sections written for one topic, in order.

Merge them into ONE continuous set of study notes:
- Remove repetition and duplicated explanations.
- Renumber sections and steps so the numbering runs cleanly from the start.
- Integrate transitions so the document reads as a single coherent explanation.
- Preserve EVERY Mermaid diagram exactly as written. Do not drop or alter them.
- Keep all detail. This is a merge, not a summary.
- Output only the final merged notes.
"""


def split_into_chunks(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Paragraph -> sentence -> hard slice, so no sentence is ever lost."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            sentences = re.split(r"(?<=[.!?])\s+", paragraph)
            buffer = ""
            for sentence in sentences:
                if buffer and len(buffer) + len(sentence) + 1 > max_chars:
                    chunks.append(buffer.strip())
                    buffer = sentence
                else:
                    buffer = f"{buffer} {sentence}".strip()
            if buffer.strip():
                chunks.append(buffer.strip())
            continue

        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph

    if current.strip():
        chunks.append(current.strip())
    return chunks


def generate_notes(text: str) -> str:
    """Map-reduce so any input length fits the model's context window."""
    if not text.strip():
        die("no text could be extracted from the source.")

    chunks = split_into_chunks(text)
    print(f"\n  Source is {len(text):,} chars -> {len(chunks)} chunk(s).")

    if len(chunks) == 1:
        print("  Single pass (fits the context window) ...")
        return chat(SYSTEM_PROMPT, f"Source material:\n\n{chunks[0]}")

    print("  Map pass - drafting notes per chunk ...")
    drafts: list[str] = []
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        print(f"  Chunk {index}/{total} ({len(chunk):,} chars)")
        instruction = (
            f"Source material (section {index} of {total}):\n\n{chunk}\n\n"
            f"This is section {index} of {total}. Do not restate material from "
            "earlier sections. Continue the numbering naturally."
        )
        drafts.append(chat(SYSTEM_PROMPT, instruction))

    print("  Reduce pass - merging drafts into one document ...")
    merged = "\n\n---\n\n".join(drafts)
    return chat(CONSOLIDATION_PROMPT, f"Draft sections:\n\n{merged}")


# ===========================================================================
# 9. MERMAID DIAGRAMS
# ===========================================================================

def _quote_labels(code: str) -> str:
    """Quote node labels containing brackets/parens - the commonest LLM error.

    ``A[Step (1)]`` is invalid Mermaid; ``A["Step (1)"]`` is valid.
    """

    def fix(match: re.Match) -> str:
        whole = match.group(0)
        if '"' in whole:
            return whole                      # already quoted
        body = whole[1:-1]
        if not re.search(r"[()\[\]{}]", body):
            return whole
        # Keep the original delimiters; only the body gets quoted.
        return f'{whole[0]}"{body.replace(chr(34), chr(39))}"{whole[-1]}'

    for pattern in (r"\[[^\[\]]*\]", r"\([^()]*\)", r"\{[^{}]*\}"):
        code = re.sub(pattern, fix, code)
    return code


def _strip_stray_gt(code: str) -> str:
    """Fix ``A -->|label|> B``, a common LLM slip that mermaid rejects.

    The edge-label pipe is valid as ``-->|label|``; a trailing ``>`` is a
    syntax error.
    """
    return re.sub(r"(\|[^|\n]*\|)\s*>+", r"\1", code)


def sanitize_mermaid(code: str) -> str:
    """Normalise LLM-authored Mermaid into something mmdc will accept."""
    lines = [
        line.rstrip()
        for line in code.strip().splitlines()
        if line.strip() and not line.strip().startswith("```")
    ]
    body = "\n".join(lines)

    first = next((ln.strip() for ln in lines if ln.strip()), "")
    # Compare only the leading token: "flowchart TD" -> "flowchart".
    first_token = first.split()[0].split("(")[0].strip().lower() if first else ""
    if first_token not in MERMAID_KEYWORDS:
        # No diagram header at all: mermaid cannot parse this, so supply one.
        body = "flowchart TD\n" + "\n".join(f"    {ln}" for ln in lines)

    body = _strip_stray_gt(body)
    return _quote_labels(body)


def salvage_mermaid(code: str) -> str:
    """Last-resort repair: keep the shape, drop the edge labels.

    A second ``flowchart TD`` header would itself be a syntax error, so this
    works on the already-sanitised text rather than re-wrapping it.
    """
    stripped = re.sub(r"\|[^|\n]*\|", "", code)
    return re.sub(r"\|\s*>*", "", stripped)


def resolve_mmdc() -> list[str]:
    """mmdc ships as a .ps1 shim on Windows; subprocess can't exec that."""
    import shutil

    for candidate in ("mmdc.cmd", "mmdc"):
        found = shutil.which(candidate)
        if found:
            return [found]
    if shutil.which("npx"):
        return ["npx", "-y", "@mermaid-js/mermaid-cli"]
    die("mermaid-cli not found. Install it with:  npm install -g @mermaid-js/mermaid-cli")


def render_mermaid(markdown_text: str) -> str:
    """Replace every mermaid fence with a rendered PNG image link."""
    pattern = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
    blocks = list(pattern.finditer(markdown_text))
    if not blocks:
        return markdown_text

    print(f"\n  Rendering {len(blocks)} Mermaid diagram(s) ...")
    mmdc = resolve_mmdc()
    pieces: list[str] = []
    cursor = 0

    for number, match in enumerate(blocks, start=1):
        pieces.append(markdown_text[cursor:match.start()])
        cursor = match.end()

        code = sanitize_mermaid(match.group(1))
        png_path = TEMP_DIR / f"diagram_{number}.png"
        mmd_path = TEMP_DIR / f"diagram_{number}.mmd"
        mmd_path.write_text(code, encoding="utf-8")

        rendered = False
        detail = "not attempted"
        # Attempt 2 is a genuine second chance: drop the edge labels, which are
        # decorative and are the most common cause of a parse failure.
        attempts = [code, salvage_mermaid(code)]
        for attempt, attempt_code in enumerate(attempts):
            if png_path.exists():
                png_path.unlink()
            mmd_path.write_text(attempt_code, encoding="utf-8")
            try:
                result = subprocess.run(
                    mmdc + [
                        "-i", str(mmd_path), "-o", str(png_path),
                        "-b", "white", "--size", "1400", "-s", "2",
                        "-q",
                    ],
                    capture_output=True, text=True, timeout=180,
                )
                if result.returncode == 0 and png_path.exists():
                    rendered = True
                    break
                message = (result.stderr or result.stdout or "").strip().splitlines()
                detail = message[-1] if message else "unknown error"
            except Exception as exc:                   # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                print(f"    diagram {number}: retrying without edge labels ({detail[:44]})")

        if rendered:
            print(f"    diagram {number}: OK -> {png_path.name}")
            try:
                relative = png_path.relative_to(BASE_DIR).as_posix()
            except ValueError:
                relative = png_path.as_posix()
            pieces.append(f"![Diagram {number}]({relative})")
        else:
            print(f"    diagram {number}: could not render ({detail[:60]})")
            pieces.append(
                f"*[Diagram {number} could not be rendered by mermaid-cli. "
                f"Mermaid source preserved below.]*\n\n"
                f"```\n{code}\n```"
            )

    pieces.append(markdown_text[cursor:])
    return "".join(pieces)


# ===========================================================================
# 10. PDF BUILDER
# ===========================================================================

class NotebookCanvas(pdfcanvas.Canvas):
    """Ruled notebook paper.

    Ordering matters, twice over.

    1. The ruling must sit BEHIND the text. Rather than painting it when a page
       starts, the ruling operators are *prepended* to the page's content
       stream just before the page is closed. Prepended operators are emitted
       first, so they paint underneath everything drawn afterwards.

    2. A page must only contain ruling if it actually receives text. If the
       ruling were drawn at page start, an unused trailing page would still hold
       ruling operators, and Canvas.save() - which only flushes a page when
       `len(self._code)` is non-zero - would write a blank final sheet. By
       adding the ruling only as a page is closed, an unused page stays empty
       and is correctly discarded.
    """

    page_count = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        NotebookCanvas.page_count += 1
        self._page_number = NotebookCanvas.page_count
        self._background_added = False
        self._prepend_background()

    def _background_code(self) -> list[str]:
        """Ruling operators, in PDF content-stream form."""
        width, height = self._pagesize
        rule = colors.HexColor(RULE_COLOR)
        margin = colors.HexColor(MARGIN_RED)

        operators = [
            "q",
            f"{rule.red:.6f} {rule.green:.6f} {rule.blue:.6f} RG",
            "0.6 w",
        ]
        y = height - PAGE_TOP - RULE_OFFSET
        while y > PAGE_BOTTOM:
            operators.append(
                f"{PAGE_LEFT - 18:.2f} {y:.2f} m {width - PAGE_RIGHT:.2f} {y:.2f} l S"
            )
            y -= RULE_GAP
        operators.append(f"{margin.red:.6f} {margin.green:.6f} {margin.blue:.6f} RG")
        operators.append("1.1 w")
        operators.append(
            f"{MARGIN_LINE_X} {PAGE_BOTTOM} m "
            f"{MARGIN_LINE_X} {height - PAGE_TOP:.2f} l S"
        )
        operators.append("Q")
        return operators

    def _prepend_background(self) -> None:
        # Page 1 is ruled in __init__ and would otherwise be ruled again when
        # its showPage() arrives.
        if self._background_added:
            return
        self._code[0:0] = self._background_code()
        self._background_added = True

    def showPage(self):
        # Page number sits in the bottom margin, drawn last so it is on top.
        self.saveState()
        self.setFont(FONT_NAME, 12)
        self.setFillColor(colors.HexColor(INK_COLOR))
        width, _ = self._pagesize
        self.drawRightString(
            width - PAGE_RIGHT, PAGE_BOTTOM / 2, f"- {self._page_number} -"
        )
        self.restoreState()

        self._prepend_background()     # behind everything on this page
        super().showPage()             # close it; the next page starts empty
        NotebookCanvas.page_count += 1
        self._page_number = NotebookCanvas.page_count
        self._background_added = False  # the next page needs its own ruling


def make_styles(font_name: str) -> dict[str, ParagraphStyle]:
    ink = colors.HexColor(INK_COLOR)
    base = ParagraphStyle(
        "Body", fontName=font_name, fontSize=FONT_SIZE, leading=LEADING,
        textColor=ink, alignment=TA_JUSTIFY, spaceAfter=6,
    )
    styles = {"body": base}
    for level, size in ((1, 22), (2, 19), (3, 17), (4, 16), (5, 16), (6, 16)):
        styles[f"h{level}"] = ParagraphStyle(
            f"Heading{level}", parent=base, fontSize=size, leading=size + 8,
            spaceBefore=12 if level <= 2 else 8, spaceAfter=6, alignment=TA_LEFT,
            # Without this a heading can be stranded alone at the foot of a page
            # while its body text starts overleaf.
            keepWithNext=1,
        )
    styles["li"] = ParagraphStyle(
        "ListItem", parent=base, alignment=TA_LEFT, leftIndent=18, spaceAfter=4,
    )
    styles["quote"] = ParagraphStyle(
        "Quote", parent=base, alignment=TA_LEFT, leftIndent=24,
        rightIndent=12, textColor=colors.HexColor("#4a5a8c"),
    )
    styles["caption"] = ParagraphStyle(
        "Caption", parent=base, fontSize=13, leading=18, alignment=TA_LEFT,
        textColor=colors.HexColor("#4a5a8c"),
    )
    # reportlab 5.x takes a ParagraphStyle here rather than font kwargs.
    styles["code"] = ParagraphStyle(
        "Code", fontName=CODE_FONT, fontSize=11, leading=14,
        textColor=colors.HexColor("#1a2a6c"),
        backColor=colors.HexColor("#f2f4f8"), borderPadding=6,
    )
    return styles


class MarkdownFlowableBuilder(HTMLParser):
    """Convert Python-Markdown HTML into ReportLab flowables.

    Purpose-built rather than reportlab.parser.parseHtml, which handles this
    markup unreliably.
    """

    def __init__(self, styles: dict[str, ParagraphStyle], font_name: str):
        super().__init__(convert_charrefs=True)
        self.styles = styles
        self.font_name = font_name
        self.flowables: list = []
        self._inline: list[str] = []
        self._block: str | None = None
        self._list_stack: list[str] = []
        self._list_index = 0
        self._pre_lines: list[str] = []
        self._in_pre = False
        self._table_rows: list[list] = []
        self._row: list | None = None
        self._cell: list[str] | None = None
        self._image: dict | None = None

    # -- helpers ---------------------------------------------------------
    def _markup(self) -> str:
        return "".join(self._inline)

    def _flush_block(self, style_key: str, bullet: str | None = None) -> None:
        """Emit the buffered inline markup as one flowable, optionally bulleted."""
        content = self._markup().strip()
        if content:
            if bullet:
                self.flowables.append(Paragraph(
                    f'<font color="#4a5a8c">{html.escape(bullet)}</font> {content}',
                    self.styles["li"],
                ))
            else:
                self.flowables.append(Paragraph(content, self.styles[style_key]))
        self._inline = []

    # -- block tags ------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._block = tag
        elif tag == "p":
            self._block = "p"
        elif tag == "blockquote":
            self._block = "blockquote"
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self._list_index = 1
        elif tag == "li":
            self._block = "li"
        elif tag == "pre":
            self._in_pre = True
            self._pre_lines = []
            self._block = "pre"
        elif tag == "code" and self._in_pre:
            pass
        elif tag == "br":
            self._inline.append("<br/>")
        elif tag == "hr":
            self.flowables.append(_Hrule())
            self.flowables.append(Spacer(1, 6))
        elif tag == "img":
            self._image = {
                "src": attrs_dict.get("src", ""),
                "alt": attrs_dict.get("alt", ""),
            }
        elif tag == "table":
            self._table_rows = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

        # inline formatting
        if tag in ("strong", "b"):
            self._inline.append("<b>")
        elif tag in ("em", "i"):
            self._inline.append("<i>")
        elif tag == "code" and not self._in_pre:
            self._inline.append(f'<font face="{CODE_FONT}" size="13">')
        elif tag == "a":
            href = attrs_dict.get("href", "")
            self._inline.append(f'<link href="{html.escape(href)}" color="#1a2a6c">')
        elif tag == "u":
            self._inline.append("<u>")

    def handle_endtag(self, tag):
        if tag in ("strong", "b"):
            self._inline.append("</b>")
        elif tag in ("em", "i"):
            self._inline.append("</i>")
        elif tag == "code" and not self._in_pre:
            self._inline.append("</font>")
        elif tag == "a":
            self._inline.append("</link>")
        elif tag == "u":
            self._inline.append("</u>")

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            content = self._markup().strip()
            if content:
                self.flowables.append(Paragraph(content, self.styles[tag]))
                if tag == "h1":
                    self.flowables.append(Spacer(1, 2))
            self._inline = []
            self._block = None
        elif tag == "p":
            if self._image is not None:
                image_flowable = self._build_image()
                if image_flowable is not None:
                    self.flowables.append(image_flowable)
                    if self._image.get("alt"):
                        self.flowables.append(
                            Paragraph(
                                html.escape(self._image["alt"]), self.styles["caption"]
                            )
                        )
            else:
                self._flush_block("body")
            self._image = None
            self._block = None
        elif tag == "blockquote":
            content = self._markup().strip()
            if content:
                self.flowables.append(Paragraph(content, self.styles["quote"]))
            self._inline = []
            self._block = None
        elif tag == "li":
            marker = "•"
            if self._list_stack and self._list_stack[-1] == "ol":
                marker = f"{self._list_index}."
                self._list_index += 1
            self._flush_block("li", marker)
            self._block = None
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._list_index = 1
        elif tag == "pre":
            text = "".join(self._pre_lines).strip("\n")
            if text:
                # XPreformatted parses its text as ReportLab markup, so any
                # angle brackets in the code (e.g. "<world>") must be escaped
                # or they are silently swallowed as unknown tags.
                self.flowables.append(
                    XPreformatted(xml_escape(text), self.styles["code"])
                )
                self.flowables.append(Spacer(1, 6))
            self._in_pre = False
            self._pre_lines = []
            self._inline = []
            self._block = None
        elif tag == "table":
            self._flush_table()
        elif tag == "tr":
            if self._row is not None:
                self._table_rows.append(self._row)
            self._row = None
        elif tag in ("td", "th"):
            if self._row is not None and self._cell is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None

    def handle_data(self, data):
        if self._in_pre:
            self._pre_lines.append(data)
            return
        if self._cell is not None:
            self._cell.append(html.escape(data))
            return
        self._inline.append(html.escape(data))

    # -- images / tables -------------------------------------------------
    def _build_image(self):
        src = (self._image or {}).get("src", "")
        path = Path(src)
        if not path.is_absolute():
            path = BASE_DIR / src
        if not path.exists():
            return None
        try:
            pixel_width, pixel_height = ImageReader(str(path)).getSize()
        except Exception:                              # noqa: BLE001
            return None
        if not pixel_width or not pixel_height:
            return None

        # The usable area is the frame box minus its padding; an image sized to
        # the full frame is rejected outright, so leave slack on both axes.
        usable_width = LETTER[0] - PAGE_LEFT - PAGE_RIGHT - 2 * FRAME_PADDING - 6
        usable_height = LETTER[1] - PAGE_TOP - PAGE_BOTTOM - 2 * FRAME_PADDING - 30
        scale = min(usable_width / pixel_width, usable_height / pixel_height, 1.0)
        return Image(
            str(path), width=pixel_width * scale, height=pixel_height * scale
        )

    def _flush_table(self):
        if not self._table_rows:
            return
        data = [
            [Paragraph(cell, self.styles["li"]) for cell in row]
            for row in self._table_rows
        ]
        column_count = max(len(row) for row in data)
        for row in data:
            row.extend([""] * (column_count - len(row)))

        available = LETTER[0] - PAGE_LEFT - PAGE_RIGHT
        table = Table(data, colWidths=[available / column_count] * column_count)
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor(RULE_COLOR)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf1fa")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        self.flowables.append(table)
        self.flowables.append(Spacer(1, 8))
        self._table_rows = []


class _Hrule(Spacer):
    """A thin horizontal divider used for <hr> and after top-level headings."""

    def __init__(self, width: int = 120):
        super().__init__(1, 8)
        self._width = width

    def draw(self):
        self.canv.setStrokeColor(colors.HexColor(RULE_COLOR))
        self.canv.setLineWidth(1.0)
        self.canv.line(0, 4, self._width, 4)


def markdown_to_flowables(markdown_text: str, styles, font_name: str) -> list:
    require(markdown_lib, "markdown", "markdown")
    html_text = markdown_lib.markdown(
        markdown_text, extensions=["tables", "fenced_code", "sane_lists"]
    )
    builder = MarkdownFlowableBuilder(styles, font_name)
    builder.feed(html_text)
    builder.close()
    return builder.flowables


def build_pdf(markdown_text: str, citations: list[dict],
              font_name: str) -> tuple[Path, int]:
    output_path = OUTPUT_DIR / PDF_NAME
    styles = make_styles(font_name)

    flowables = markdown_to_flowables(markdown_text, styles, font_name)
    if not flowables:
        flowables = [Paragraph("No content was produced.", styles["body"])]

    if citations:
        flowables.append(Spacer(1, 16))
        flowables.append(Paragraph("Sources", styles["h2"]))
        flowables.append(Spacer(1, 4))
        for citation in citations:
            label = html.escape(f"[{citation['n']}] {citation['title']}")
            url = html.escape(citation["url"])
            flowables.append(Paragraph(
                f'<font size="13">{label}<br/>'
                f'<link href="{url}" color="#4a5a8c">{url}</link></font>',
                styles["li"],
            ))

    NotebookCanvas.page_count = 0
    document = SimpleDocTemplate(
        str(output_path), pagesize=LETTER,
        leftMargin=PAGE_LEFT, rightMargin=PAGE_RIGHT,
        topMargin=PAGE_TOP, bottomMargin=PAGE_BOTTOM,
        title="Study Notes", author="handwritten-notes",
    )
    document.build(flowables, canvasmaker=NotebookCanvas)
    # document.page counts pages actually emitted, unlike the canvas counter.
    return output_path, document.page


# --- browser preview -------------------------------------------------------

# The same six tokens the PDF already ships, so the on-screen page and the
# printed page are one object. KALAM_CSS is fetched by the web front end too.
PREVIEW_CSS = """
:root{
  --paper:#fbfcfe; --rule:#a8c8e8; --ink:#1a2a6c;
  --margin:#e06a6a; --graphite:#5a6480; --pencil:#c9d4e4;
  --line:24px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:17px/var(--line) "IBM Plex Sans",-apple-system,"Segoe UI",sans-serif;
  padding:calc(var(--line) * 2) 24px calc(var(--line) * 3);
}
.sheet{
  max-width:44rem; margin:0 auto; position:relative;
  background-image:repeating-linear-gradient(
    to bottom, transparent 0, transparent calc(var(--line) - 1px),
    var(--rule) calc(var(--line) - 1px), var(--rule) var(--line));
}
.sheet::before{
  content:""; position:absolute; top:0; bottom:0; left:64px; width:1px;
  background:var(--margin); opacity:.85;
}
body > *{max-width:44rem; margin-left:auto; margin-right:auto}
h1,h2,h3{font-weight:600; line-height:var(--line); margin:0}
h1{font-size:1.6rem; margin-bottom:calc(var(--line) * -.25)}
h2{font-size:1.28rem}
h3{font-size:1.08rem}
p,ul,ol,pre,table{margin:0}
p,li{padding-left:80px}
ul,ol{padding:0; list-style:none}
li{position:relative}
li::before{content:"\\2022"; position:absolute; left:62px; color:var(--ink)}
ol{counter-reset:step}
ol li{counter-increment:step}
ol li::before{content:counter(step) "."; font-size:.85em; left:56px}
a{color:var(--ink); text-underline-offset:3px}
strong{font-weight:600}
hr{border:0; height:1px; background:var(--rule); margin:0}
pre{
  background:rgba(255,255,255,.72); border-left:2px solid var(--rule);
  padding:0 12px 0 14px; overflow-x:auto; white-space:pre-wrap;
  font:13px/20px "Courier New",monospace; color:var(--ink);
}
code{font:13px/20px "Courier New",monospace; background:rgba(168,200,232,.28)}
pre code{background:none}
blockquote{margin:0; padding-left:80px; color:var(--graphite); font-style:italic}
img{
  display:block; margin:0 0 0 80px; max-width:calc(100% - 80px);
  background:#fff; border:1px solid var(--rule);
}
table{border-collapse:collapse; padding-left:80px}
th,td{border:1px solid var(--rule); padding:2px 10px; text-align:left}
th{background:rgba(168,200,232,.24); font-weight:600}
.sources{padding-left:80px; font-size:.88rem; line-height:20px}
.sources h2{font-size:1.05rem}
.sources a{color:var(--graphite)}
@media print{
  body{padding:0}
  .sheet::before{display:none}
}
"""

PREVIEW_FONT_CSS = (
    '@import url("https://fonts.googleapis.com/css2?'
    'family=IBM+Plex+Sans:wght@400;500;600&'
    'family=Kalam:wght@400;700&display=swap");'
)


def _source_item(citation: dict) -> str:
    """One Sources entry: linked title, then the bare URL underneath."""
    url = html.escape(citation["url"], quote=True)
    label = html.escape(f'[{citation["n"]}] {citation["title"]}')
    return (
        f'<li><a href="{url}" rel="noopener">{label}</a>'
        f"<br>{url}</li>"
    )


def build_html_preview(markdown_text: str, citations: list[dict],
                       title: str = "Study Notes") -> Path:
    """A readable on-screen version of the notes, styled as the same notebook.

    Written next to the PDF in output/. Diagram links are rebased because the
    prepared markdown points at temp/diagram_N.png relative to BASE_DIR.
    """
    require(markdown_lib, "markdown", "markdown")
    output_path = OUTPUT_DIR / HTML_NAME

    body = markdown_lib.markdown(
        markdown_text, extensions=["tables", "fenced_code", "sane_lists"]
    )

    sources = ""
    if citations:
        items = "\n".join(_source_item(c) for c in citations)
        sources = f'<div class="sources"><h2>Sources</h2><ol>{items}</ol></div>'

    # temp/diagram_1.png -> ../temp/diagram_1.png
    def _rebase(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith(("http://", "https://", "data:", "/")):
            return match.group(0)
        return f'src="{html.escape("../" + src, quote=True)}"'

    body = re.sub(r'src="([^"]+)"', _rebase, body)

    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{PREVIEW_FONT_CSS}
{PREVIEW_CSS}</style>
</head>
<body>
<article class="sheet">
{body}
{sources}
</article>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")
    return output_path


# ===========================================================================
# 11. ORCHESTRATION
# ===========================================================================

def run_pipeline(text: str, citations: list[dict],
                 font_name: str) -> tuple[Path, int]:
    announce_stage("notes")
    banner("Generating study notes")
    notes = generate_notes(text)

    announce_stage("diagrams")
    banner("Rendering Mermaid diagrams")
    prepared = render_mermaid(notes)

    announce_stage("pdf")
    banner("Building PDF")
    output_path, page_count = build_pdf(prepared, citations, font_name)

    # The browser reads the notes without waiting on the file, so it is built
    # from the same prepared markdown the PDF used.
    announce_stage("preview")
    try:
        html_path = build_html_preview(prepared, citations)
    except Exception as exc:                           # noqa: BLE001
        print(f"  HTML preview unavailable: {exc}")
    else:
        announce_artifact(
            "html", html_path, bytes=html_path.stat().st_size
        )

    announce_artifact(
        "pdf", output_path, pages=page_count,
        bytes=output_path.stat().st_size,
    )
    return output_path, page_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a source into handwritten-style PDF study notes.",
        epilog="With no argument you get an interactive prompt. A bare topic "
               "triggers consent-gated web research; a URL or file does not.",
    )
    parser.add_argument(
        "input", nargs="?", default=None,
        help="YouTube URL, local media/document path, or a topic to research",
    )
    args = parser.parse_args()

    ensure_dirs()
    banner("Handwritten PDF Study Notes Generator")
    font_name = ensure_handwritten_font()
    ensure_llm_ready()

    pending = args.input
    citations: list[dict] = []
    text: str | None = None

    while True:
        # ---------------- get a source ----------------
        while text is None:
            if pending is not None:
                raw, pending = pending, None
            else:
                banner("Enter a source")
                print("  - YouTube URL or playlist link")
                print("  - Local file: .mp3 .wav .mp4 .m4a .mkv .webm .mov .flac .ogg")
                print("           or .pdf .txt .md")
                print("  - A bare topic, which will ask before searching the web")
                print()
                try:
                    announce_prompt("await_source")
                    raw = input("  Source (blank to quit): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if not raw:
                    print("  Bye.")
                    return 0

            kind, payload = classify_input(raw)
            citations = []

            if kind == "youtube":
                announce_stage("download")
                banner("YouTube source")
                print(f"  {payload}")
                print("  Downloading audio with yt-dlp ...")
                try:
                    paths = download_youtube_audio(str(payload))
                except Exception as exc:               # noqa: BLE001
                    print(f"  Download failed: {exc}")
                    continue
                if not paths:
                    print("  No audio was downloaded.")
                    continue
                announce_stage("transcribe")
                text = transcribe_media(paths)

            elif kind == "media":
                announce_stage("download")
                banner("Local media file")
                print(f"  {payload}")
                announce_stage("transcribe")
                text = transcribe_media([payload])

            elif kind == "document":
                path = Path(payload)
                announce_stage("extract")
                banner("Document source")
                print(f"  {path}")
                if path.suffix.lower() == ".pdf":
                    text = extract_pdf_text(path)
                else:
                    text = extract_txt_text(path)
                print(f"  Extracted {len(text):,} characters.")

            else:  # topic -> consent-gated research
                announce_stage("research")
                banner("Topic source")
                result = gather_sources(str(payload))
                if result is None:
                    print("\n  No source attached and web research declined.")
                    print("  Provide a YouTube URL or a local file path instead.")
                    continue
                text, citations = result

        # ---------------- build ----------------
        try:
            output_path, page_count = run_pipeline(text, citations, font_name)
        except KeyboardInterrupt:
            print("\n  Cancelled.")
            return 130
        except Exception as exc:                       # noqa: BLE001
            print(f"\n  Could not build the PDF: {type(exc).__name__}: {exc}")
            try:
                announce_prompt("retry")
                retry = input("  Try another source? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
            if retry in ("y", "yes", ""):
                text = None
                continue
            return 1

        banner("Done")
        print(f"  PDF    : {output_path}")
        print(f"  Pages  : {page_count}")
        print(f"  Size   : {human_size(output_path.stat().st_size)}")
        if citations:
            print(f"  Sources: {len(citations)} cited in the document")
        print(f"\n  Open it with:  start \"{output_path}\"")

        text = None
        try:
            announce_prompt("done_again")
            again = input("\n  Generate another set of notes? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if again not in ("y", "yes"):
            print("  Bye.")
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
