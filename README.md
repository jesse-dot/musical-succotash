# 🐾 Kemono Fursuiter Dance Rater

A Python + Flask web app that:

1. **Generates search terms** using the Google Gemini API
2. **Finds YouTube videos** of Kemono fursuiters dancing via yt-dlp
3. **AI-rates each video** with Gemini (scoring *Dance Energy* and *Cuteness* from 1–5 using the video thumbnail + metadata)
4. **Streams results live** to a responsive web UI using Server-Sent Events — each card appears the moment its ratings are ready

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/jesse-dot/musical-succotash.git
cd musical-succotash
pip install -r requirements.txt
```

### 2. Set your Gemini API key

```bash
cp .env.example .env
# then edit .env and replace the placeholder with your real key
```

Get a free key at <https://aistudio.google.com/app/apikey>.

### 3. Run the server

```bash
python app.py
```

Open <http://localhost:5000> in your browser.

### 4. Find & rate videos

Click **▶ Find & Rate Videos** in the web UI. Gemini will generate search terms, yt-dlp will search YouTube, and each video will be rated and displayed as soon as it's ready.

---

## How it works

| Component | Technology |
|-----------|-----------|
| Web framework | Flask |
| Video search | yt-dlp (YouTube) |
| AI search terms + ratings | Google Gemini 2.0 Flash |
| Live UI updates | Server-Sent Events (SSE) |
| Persistence | SQLite (`videos.db`) |

### Rating categories

| Category | What it measures |
|---|---|
| ⚡ **Dance Energy** | How energetic, skilful, and dynamic the dancing is |
| 💖 **Cuteness** | How cute and adorable the fursuit / character is |

Each score is 1–5 (★☆☆☆☆ → ★★★★★).

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | ✅ Yes | Google Gemini API key |