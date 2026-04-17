# 🐾 Kemono Fursuiter Dance Rater

A Python + Flask web app that runs **100% locally — no cloud AI keys required**:

1. **Generates search terms** using a local Ollama model
2. **Finds YouTube videos** of Kemono fursuiters dancing via yt-dlp
3. **AI-rates each video** locally (scoring *Dance Energy* and *Cuteness* from 1–5 using the video thumbnail + metadata)
4. **Streams results live** to a responsive web UI using Server-Sent Events — each card appears the moment its ratings are ready

Designed to run on low-power hardware — the default model (**moondream**, 1.7B params, ~1.1 GB) runs comfortably on a **Raspberry Pi 4** (4 GB RAM).

---

## Quick start

### 1. Install Ollama and pull the model

```bash
# Install Ollama: https://ollama.com/download
ollama pull moondream   # ~1.1 GB download — runs on Raspberry Pi 4+
```

### 2. Clone and install Python dependencies

```bash
git clone https://github.com/jesse-dot/musical-succotash.git
cd musical-succotash
pip install -r requirements.txt
```

### 3. (Optional) Configure via .env

```bash
cp .env.example .env
# Edit .env to change OLLAMA_HOST or OLLAMA_MODEL if needed
```

### 4. Run the server

```bash
python app.py
```

Open <http://localhost:5000> in your browser.

### 5. Find & rate videos

Click **▶ Find & Rate Videos**. The local model generates search terms, yt-dlp searches YouTube, and each video is rated and displayed as soon as it's ready.

---

## How it works

| Component | Technology |
|-----------|-----------|
| Web framework | Flask |
| Video search | yt-dlp (YouTube) |
| AI search terms + ratings | Ollama — **moondream** (local, no API key) |
| Live UI updates | Server-Sent Events (SSE) |
| Persistence | SQLite (`videos.db`) |

### Rating categories

| Category | What it measures |
|---|---|
| ⚡ **Dance Energy** | How energetic, skilful, and dynamic the dancing is |
| 💖 **Cuteness** | How cute and adorable the fursuit / character is |

Each score is 1–5 (★☆☆☆☆ → ★★★★★).

---

## Configuration

All settings are optional — the defaults work out of the box.

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `moondream` | Model for search terms + rating |

### Choosing a model

| Model | Size | RAM needed | Notes |
|---|---|---|---|
| `moondream` ✅ | ~1.1 GB | ≥ 2 GB | **Default.** Runs on Raspberry Pi 4. Fast. |
| `llava:7b` | ~4.7 GB | ≥ 6 GB | Better accuracy, needs a real PC/laptop |
| `llama3.2-vision` | ~2.0 GB | ≥ 4 GB | Good balance of size and quality |

Set your preferred model: `OLLAMA_MODEL=llava:7b python app.py`