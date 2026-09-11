"""
Word MP3 Generator — PET teacher tool
Input: list of up to 15 words → Output: numbered MP3, each word read 3× with pauses
"""

import os
import uuid
import subprocess
import tempfile
from datetime import datetime
from elevenlabs import ElevenLabs
from flask import (
    Flask, render_template, request, jsonify,
    send_from_directory, abort
)

# ── Config ──────────────────────────────────────────────────────
APP_DIR     = os.path.dirname(os.path.abspath(__file__))
OUT_DIR     = os.path.join(APP_DIR, "static", "downloads")
TEMPLATE    = os.path.join(APP_DIR, "templates", "index.html")
STATIC      = os.path.join(APP_DIR, "static")
NUM_REPEATS = 3          # times each word is read
PAUSE_NUM   = 2   # seconds between "Number N" and first word
PAUSE_REP   = 3   # seconds between repetitions of the same word
PAUSE_AFTER = 6   # seconds after the last repetition, before next Number

os.makedirs(OUT_DIR, exist_ok=True)

app = Flask(__name__, template_folder=os.path.dirname(TEMPLATE),
            static_folder=STATIC)

_api_key = os.environ.get("ELEVENLABS_API_KEY", "")


def set_api_key(key):
    global _api_key
    _api_key = key.strip()
    os.environ["ELEVENLABS_API_KEY"] = _api_key


# ── Audio helpers ───────────────────────────────────────────────

def synthesize(text: str, api_key: str, voice_id: str, speed: float = 0.8) -> bytes:
    """Call ElevenLabs TTS, return raw MP3 bytes."""
    client = ElevenLabs(api_key=api_key)
    audio_iter = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id="eleven_flash_v2_5",
        voice_settings={"speed": speed},
    )
    return b"".join(audio_iter)


def write_temp(data: bytes) -> str:
    """Write bytes to a temp file, return path."""
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.write(fd, data)
    os.close(fd)
    return path


def generate_silence(duration_s: float) -> str:
    """Generate a silent MP3 of given duration, return temp file path."""
    path = f"/tmp/silence_{uuid.uuid4().hex}.mp3"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=mono",
        "-t", str(duration_s),
        "-q:a", "9",
        path
    ], capture_output=True)
    return path


def concat_ffmpeg(file_list: list, output_path: str):
    """Concatenate a list of MP3 files into output_path using ffmpeg."""
    manifest = f"/tmp/concat_{uuid.uuid4().hex}.txt"
    with open(manifest, "w") as f:
        for p in file_list:
            f.write(f"file '{p}'\n")

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", manifest,
        "-c", "copy",
        output_path
    ], capture_output=True)

    os.unlink(manifest)


def build_word_segment(word: str, num: int, api_key: str, voice_id: str,
                       speed: float) -> list:
    """
    Build audio pieces for one word:
      Number N → [pause NUM] → word → [pause REP] → word → [pause AFTER] → word
    Returns list of temp file paths.
    """
    pieces = []

    num_audio = synthesize(f"Number {num}", api_key, voice_id, speed)
    num_path  = write_temp(num_audio)
    pieces.append(num_path)

    # 2s pause after Number N, before first word
    pieces.append(generate_silence(PAUSE_NUM))

    for i in range(NUM_REPEATS):
        word_audio = synthesize(word.strip(), api_key, voice_id, speed)
        word_path  = write_temp(word_audio)
        pieces.append(word_path)
        # After last repetition: 6s pause before next Number; else: 3s between reps
        if i < NUM_REPEATS - 1:
            pieces.append(generate_silence(PAUSE_REP))
        else:
            pieces.append(generate_silence(PAUSE_AFTER))

    return pieces


def generate_mp3(words: list, api_key: str, voice_id: str,
                 filename: str, speed: float = 0.8) -> str:
    """Generate the full MP3, save to OUT_DIR, return full path."""
    all_pieces = []
    total      = len(words)

    for i, word in enumerate(words):
        _log(f"  [{i+1}/{total}] synthesizing: {word}")
        pieces = build_word_segment(word, i + 1, api_key, voice_id, speed)
        all_pieces.extend(pieces)

    out_path = os.path.join(OUT_DIR, filename)
    concat_ffmpeg(all_pieces, out_path)

    for p in all_pieces:
        try: os.unlink(p)
        except: pass

    _log(f"  → saved: {out_path}")
    return out_path


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(force=True) or {}

    words    = [w.strip() for w in data.get("words", "").strip().split("\n") if w.strip()][:15]
    voice_id = data.get("voice_id", "")
    api_key  = (data.get("api_key") or _api_key or "").strip()
    speed    = float(data.get("speed", 0.8))

    if not api_key:
        return jsonify({"error": "No API key. Go to Settings → enter your ElevenLabs key."}), 400
    if not voice_id:
        return jsonify({"error": "No voice selected. Go to Settings → Load voices → pick one."}), 400
    if not words:
        return jsonify({"error": "No words entered."}), 400

    _log(f"Generate request: {len(words)} words, voice={voice_id}, speed={speed}")

    filename = f"words_{uuid.uuid4().hex[:8]}.mp3"
    try:
        generate_mp3(words, api_key, voice_id, filename, speed)
    except Exception as e:
        _log(f"Error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "download_url": f"/static/downloads/{filename}",
        "word_count": len(words)
    })


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(force=True) or {}
    key  = (data.get("api_key") or "").strip()
    if not key:
        return jsonify({"error": "API key required."}), 400
    set_api_key(key)
    _log("API key updated.")
    return jsonify({"ok": True})


@app.route("/api/voices", methods=["GET"])
def api_voices():
    api_key = (request.args.get("api_key") or _api_key or "").strip()
    if not api_key:
        return jsonify({"error": "No API key."}), 400
    try:
        client = ElevenLabs(api_key=api_key)
        result = client.voices.get_all()
        voices = result.voices or []
        return jsonify({
            "voices": [
                {
                    "voice_id": v.voice_id,
                    "name":     v.name,
                    "labels":   getattr(v, "labels", None) or {},
                    "preview":  getattr(v, "preview_url", None) or ""
                }
                for v in voices
            ]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── Run ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    _log(f"API key loaded: {'YES' if _api_key else 'NO — enter in Settings'}")
    _log(f"Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
