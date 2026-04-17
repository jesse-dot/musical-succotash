"""
Kemono Fursuiter Dance Rater
Searches YouTube for kemono fursuiter dancing videos, rates them with Gemini AI
(Dance Energy 1-5 and Cuteness 1-5), and streams results live to a web UI.
"""

import json
import os
import queue
import re
import sqlite3
import threading
import time
from io import BytesIO

import google.genai as genai
from google.genai import types as genai_types
import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template
from PIL import Image

load_dotenv()

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


def _configure_gemini() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. "
            "Add it to a .env file or export it as an environment variable."
        )
    return genai.Client(api_key=api_key)


def _generate_search_terms(client: genai.Client) -> list[str]:
    prompt = (
        "Generate 6 different YouTube search query strings to find videos of "
        "kemono fursuiters dancing. Kemono fursuits are Japanese-style cute "
        "animal costume characters. Mix up the search terms to get diverse "
        "results (e.g. include terms for specific dances, events, or styles). "
        "Return ONLY the search terms, one per line, with no numbering, "
        "no bullets, and no extra text."
    )
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )
    terms = [
        line.strip()
        for line in response.text.strip().splitlines()
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


def _rate_video(
    client: genai.Client,
    video_id: str,
    title: str,
    thumbnail_url: str,
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

    img = _fetch_thumbnail(thumbnail_url)

    try:
        if img:
            # Convert PIL image to bytes for the new SDK
            buf = BytesIO()
            img.save(buf, format="JPEG")
            image_bytes = buf.getvalue()
            contents = [
                genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                base_prompt,
            ]
        else:
            contents = base_prompt

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents,
        )

        text = response.text.strip()
        match = re.search(r"\{[^}]+\}", text, re.DOTALL)
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
        client = _configure_gemini()
    except RuntimeError as exc:
        broadcast({"type": "error", "message": str(exc)})
        return

    broadcast({"type": "status", "message": "Asking Gemini for search terms…"})

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

            broadcast(
                {
                    "type": "status",
                    "message": f"Rating: {title[:60]}…",
                }
            )

            dance_energy, cuteness = _rate_video(
                client, video_id, title, thumbnail, description
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

            # Small pause to avoid hammering Gemini API
            time.sleep(1.5)

    broadcast({"type": "done", "message": "All videos rated!"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
