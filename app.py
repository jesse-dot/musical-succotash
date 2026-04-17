"""
Kemono Fursuiter Dance Rater
Searches YouTube for kemono fursuiter dancing videos, rates them with a local
Ollama AI model (Dance Energy 1-5 and Cuteness 1-5), and streams results live
to a web UI.  No cloud AI keys required — everything runs on your machine.
"""

import json
import os
import queue
import re
import sqlite3
import threading
import time
from io import BytesIO
from urllib.parse import urlparse, urlunparse

import ollama as ollama_lib
import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template
from PIL import Image

load_dotenv()

# ---------------------------------------------------------------------------
# Ollama configuration (override via environment variables or .env)
# ---------------------------------------------------------------------------

def _normalize_ollama_host(raw_host: str) -> str:
    """Normalize Ollama host and auto-fix common local misconfiguration."""
    candidate = (raw_host or "").strip() or "http://localhost:11434"
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    scheme = parsed.scheme or "http"
    hostname = parsed.hostname or "localhost"
    port = parsed.port
    path = parsed.path if parsed.path not in ("", "/") else ""

    # 0.0.0.0 is valid for server bind, but not for client connect.
    if hostname == "0.0.0.0":
        hostname = "localhost"

    netloc = hostname
    if port:
        netloc = f"{netloc}:{port}"

    return urlunparse((scheme, netloc, path, "", "", ""))


# URL of your local Ollama server (default: http://localhost:11434).
# If configured as 0.0.0.0:11434, it is auto-corrected to localhost.
OLLAMA_HOST_CONFIGURED = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_HOST = _normalize_ollama_host(OLLAMA_HOST_CONFIGURED)

# Model to use for both search-term generation and video rating.
# Must support vision (image input) for thumbnail analysis.
# Default: moondream — a tiny 1.7B vision model (~1.1 GB) that runs on
# Raspberry Pi 4 (4 GB RAM) and other low-power devices.
# Other options: llava:7b, llama3.2-vision (require more RAM)
# Pull the model first:  ollama pull moondream
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "moondream")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(__file__), "videos.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id          TEXT PRIMARY KEY,
                title       TEXT,
                url         TEXT,
                thumbnail   TEXT,
                channel     TEXT,
                dance_energy INTEGER,
                cuteness    INTEGER,
                description TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Server-Sent Events broadcast
# ---------------------------------------------------------------------------

_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def broadcast(data: dict):
    payload = json.dumps(data)
    with _clients_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/videos")
def api_videos():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM videos ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events endpoint — pushes new video events to the browser."""
    client_q: queue.Queue = queue.Queue(maxsize=100)
    with _clients_lock:
        _clients.append(client_q)

    def generate():
        # Keep-alive comment so browsers don't time out immediately
        yield ": keep-alive\n\n"
        try:
            while True:
                try:
                    payload = client_q.get(timeout=25)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _clients_lock:
                if client_q in _clients:
                    _clients.remove(client_q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/start", methods=["POST"])
def api_start():
    """Kick off the background search-and-rate worker (only one at a time)."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return jsonify({"status": "already_running"})
        _worker_thread = threading.Thread(
            target=_search_and_rate, daemon=True
        )
        _worker_thread.start()
    return jsonify({"status": "started"})


@app.route("/api/status")
def api_status():
    running = bool(_worker_thread and _worker_thread.is_alive())
    return jsonify({"running": running})


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


def _configure_ollama() -> ollama_lib.Client:
    """Create an Ollama client and verify the server is reachable."""
    client = ollama_lib.Client(host=OLLAMA_HOST)
    try:
        client.list()
    except Exception as exc:
        normalization_note = ""
        if OLLAMA_HOST_CONFIGURED != OLLAMA_HOST:
            normalization_note = (
                f"\nConfigured OLLAMA_HOST was '{OLLAMA_HOST_CONFIGURED}' "
                f"(auto-normalized to '{OLLAMA_HOST}')."
            )
        raise RuntimeError(
            f"Cannot connect to Ollama at {OLLAMA_HOST}. "
            f"{normalization_note}"
            f"Make sure Ollama is installed and running:\n"
            f"  https://ollama.com/download\n"
            f"  ollama serve\n"
            f"  ollama pull {OLLAMA_MODEL}\n"
            f"Error: {exc}"
        )
    return client


def _generate_search_terms(client: ollama_lib.Client) -> list[str]:
    prompt = (
        "Generate 6 different YouTube search query strings to find videos of "
        "kemono fursuiters dancing. Kemono fursuits are Japanese-style cute "
        "animal costume characters. Mix up the search terms to get diverse "
        "results (e.g. include terms for specific dances, events, or styles). "
        "Return ONLY the search terms, one per line, with no numbering, "
        "no bullets, and no extra text."
    )
    response = client.generate(model=OLLAMA_MODEL, prompt=prompt)
    reply_text = response.response  # GenerateResponse.response holds the text
    terms = [
        line.strip()
        for line in reply_text.strip().splitlines()
        if line.strip()
    ]
    return terms[:6]  # Safety cap


def _fetch_thumbnail(url: str) -> Image.Image | None:
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None


def _sample_evenly(items: list[str], max_count: int) -> list[str]:
    if len(items) <= max_count:
        return items

    if max_count <= 1:
        return [items[0]]

    idxs = []
    for i in range(max_count):
        idx = round(i * (len(items) - 1) / (max_count - 1))
        if idx not in idxs:
            idxs.append(idx)
    return [items[i] for i in idxs]


def _get_video_context(video_id: str) -> tuple[list[str], str]:
    """
    Fetch richer metadata for a specific video and return:
      - image URLs sampled across available video images/thumbnails
      - best available description text
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception:
        return [], ""

    image_urls: list[str] = []
    for item in info.get("thumbnails") or []:
        image_url = item.get("url")
        if image_url and image_url not in image_urls:
            image_urls.append(image_url)

    return _sample_evenly(image_urls, max_count=4), info.get("description") or ""


def _rate_video(
    client: ollama_lib.Client,
    video_id: str,
    title: str,
    image_urls: list[str],
    description: str,
) -> tuple[int, int] | tuple[None, None]:
    """Return (dance_energy, cuteness) both 1-5, or (None, None) on failure."""
    snippet = (description or "")[:300]
    json_schema = '{"dance_energy": <1-5>, "cuteness": <1-5>}'
    base_prompt = (
        f"You are rating a kemono fursuiter dancing video.\n"
        f"Title: {title}\n"
        f"Description snippet: {snippet}\n\n"
        "Rate the video from 1 to 5 for each of the following categories:\n"
        "- dance_energy: How energetic, skilful, and dynamic the dancing appears.\n"
        "- cuteness: How cute and adorable the fursuit / character is.\n\n"
        f"Reply ONLY with valid JSON matching this schema: {json_schema}\n"
        "Use integer values only."
    )

    try:
        image_bytes_list: list[bytes] = []
        for image_url in image_urls:
            img = _fetch_thumbnail(image_url)
            if not img:
                continue
            buf = BytesIO()
            img.save(buf, format="JPEG")
            image_bytes_list.append(buf.getvalue())

        if image_bytes_list:
            response = client.generate(
                model=OLLAMA_MODEL,
                prompt=base_prompt,
                images=image_bytes_list,
            )
        else:
            response = client.generate(model=OLLAMA_MODEL, prompt=base_prompt)

        reply_text = response.response  # GenerateResponse.response holds the text
        match = re.search(r"\{[^}]+\}", reply_text.strip(), re.DOTALL)
        if match:
            data = json.loads(match.group())
            dance_energy = max(1, min(5, int(data.get("dance_energy", 3))))
            cuteness = max(1, min(5, int(data.get("cuteness", 3))))
            return dance_energy, cuteness
    except Exception as exc:
        print(f"[rater] error for {video_id}: {exc}")

    return None, None


def _save_video(video: dict):
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO videos
                (id, title, url, thumbnail, channel, dance_energy, cuteness, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video["id"],
                video["title"],
                video["url"],
                video["thumbnail"],
                video["channel"],
                video["dance_energy"],
                video["cuteness"],
                video["description"],
            ),
        )
        conn.commit()


def _search_and_rate():
    """Main background task: generate terms → search → rate → broadcast."""
    try:
        client = _configure_ollama()
    except RuntimeError as exc:
        broadcast({"type": "error", "message": str(exc)})
        return

    broadcast({"type": "status", "message": f"Asking {OLLAMA_MODEL} for search terms…"})

    try:
        search_terms = _generate_search_terms(client)
    except Exception as exc:
        broadcast({"type": "error", "message": f"Failed to generate search terms: {exc}"})
        return

    broadcast(
        {
            "type": "status",
            "message": f"Generated {len(search_terms)} search terms. Searching YouTube…",
        }
    )

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }

    seen_ids: set[str] = set()

    # Seed seen_ids with already-rated videos so we don't re-rate
    with get_db() as conn:
        for row in conn.execute("SELECT id FROM videos"):
            seen_ids.add(row["id"])

    for term in search_terms:
        broadcast({"type": "status", "message": f'Searching: "{term}"'})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                results = ydl.extract_info(f"ytsearch8:{term}", download=False)
                entries = results.get("entries", []) if results else []
        except Exception as exc:
            broadcast({"type": "error", "message": f"Search error: {exc}"})
            continue

        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id") or entry.get("url", "").split("v=")[-1]
            if not video_id or video_id in seen_ids:
                continue
            seen_ids.add(video_id)

            title = entry.get("title", "(no title)")
            url = f"https://www.youtube.com/watch?v={video_id}"
            thumbnail = (
                entry.get("thumbnail")
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            )
            channel = entry.get("uploader") or entry.get("channel", "Unknown")
            description = entry.get("description") or ""
            context_images, rich_description = _get_video_context(video_id)
            if rich_description:
                description = rich_description
            if not context_images and thumbnail:
                context_images = [thumbnail]
            if context_images:
                thumbnail = context_images[0]

            broadcast(
                {
                    "type": "status",
                    "message": f"Rating: {title[:60]}…",
                }
            )

            dance_energy, cuteness = _rate_video(
                client, video_id, title, context_images, description
            )
            if dance_energy is None:
                broadcast(
                    {
                        "type": "status",
                        "message": f"Skipped (rating failed): {title[:50]}",
                    }
                )
                continue

            video = {
                "id": video_id,
                "title": title,
                "url": url,
                "thumbnail": thumbnail,
                "channel": channel,
                "dance_energy": dance_energy,
                "cuteness": cuteness,
                "description": description,
            }
            _save_video(video)
            broadcast({"type": "new_video", "video": video})

            # Small pause to avoid overwhelming a low-power Ollama server
            time.sleep(1.5)

    broadcast({"type": "done", "message": "All videos rated!"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
