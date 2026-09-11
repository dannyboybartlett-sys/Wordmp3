#!/usr/bin/env python3
"""WordMP3 backend server — proxies ElevenLabs API with CORS support.
   Handles /api/voices and /api/generate routes expected by the HTML template."""
import json, sys, os, tempfile, base64, re, subprocess, wave
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ELEVENLABS_BASE = "https://api.elevenlabs.io"

# ── Number to words ───────────────────────────────────────────
NUMBERS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four",
    5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
    14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen",
    18: "eighteen", 19: "nineteen", 20: "twenty", 30: "thirty",
    40: "forty", 50: "fifty", 60: "sixty", 70: "seventy",
    80: "eighty", 90: "ninety", 100: "hundred",
}
def _num_words(n):
    n = int(n)
    if n < 20: return NUMBERS.get(n, str(n))
    if n < 100:
        rest = _num_words(n % 10)
        return (NUMBERS[n//10*10] + (" " + rest if rest != "zero" else "")).strip()
    if n < 1000:
        return (NUMBERS[n//100] + " hundred " + _num_words(n % 100)).strip()
    return str(n)

def _convert_numbers_in_phrase(text):
    """Replace ALL bare numbers in a phrase with word equivalents."""
    def repl(m):
        try:
            return _num_words(int(m.group()))
        except ValueError:
            return m.group()
    return re.sub(r'\b\d+\b', repl, text)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[server] {args[0]}")

    def _cors(self):
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, xi-api-key",
        }

    def do_OPTIONS(self):
        self.send_response(200)
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_bytes(self, method, path, headers=None, body=None):
        url = f"{ELEVENLABS_BASE}{path}"
        req = Request(url, data=body, headers=headers or {}, method=method)
        with urlopen(req, timeout=120) as resp:
            return resp.read()

    def _make_silence_mp3(self, duration_sec):
        """Generate valid MP3 silence using ffmpeg."""
        tmp_wav = tempfile.mktemp(suffix='.wav')
        tmp_mp3 = tempfile.mktemp(suffix='.mp3')
        try:
            with wave.open(tmp_wav, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
                num_frames = int(44100 * duration_sec)
                w.writeframes(b'\x00' * (num_frames * 2))
            subprocess.run([
                'ffmpeg', '-y', '-loglevel', 'quiet',
                '-f', 'wav', '-i', tmp_wav,
                '-codec:a', 'libmp3lame', '-b:a', '64k', tmp_mp3
            ], check=True)
            with open(tmp_mp3, 'rb') as f:
                return f.read()
        finally:
            for f in (tmp_wav, tmp_mp3):
                if os.path.exists(f):
                    os.remove(f)

    def _tempo_shift_mp3(self, mp3_bytes, factor):
        """Slow down / speed up MP3 audio by tempo factor (0.75 = 25% slower)."""
        tmp_in  = tempfile.mktemp(suffix='.mp3')
        tmp_out = tempfile.mktemp(suffix='.mp3')
        try:
            with open(tmp_in, 'wb') as f:
                f.write(mp3_bytes)
            subprocess.run([
                'ffmpeg', '-y', '-loglevel', 'quiet',
                '-f', 'mp3', '-i', tmp_in,
                '-filter:a', f'atempo={factor}',
                '-codec:a', 'libmp3lame', '-b:a', '96k',
                tmp_out
            ], check=True)
            with open(tmp_out, 'rb') as f:
                return f.read()
        finally:
            for f in (tmp_in, tmp_out):
                if os.path.exists(f):
                    os.remove(f)

    # ── /api/voices ──────────────────────────────────────────
    def do_GET(self):
        if not self.path.startswith("/api/voices"):
            self._json(404, {"error": f"Unknown route: {self.path}"})
            return
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        api_key = qs.get("api_key", [""])[0]
        if not api_key:
            self._json(400, {"error": "Missing api_key parameter"})
            return
        try:
            req = Request(f"{ELEVENLABS_BASE}/v1/voices",
                          headers={"xi-api-key": api_key})
            with urlopen(req, timeout=60) as resp:
                body = resp.read()
                self.send_response(200)
                for k, v in self._cors().items():
                    self.send_header(k, v)
                self.send_header("Content-Type",
                                 dict(resp.headers).get("content-type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except HTTPError as e:
            err = e.read()
            self.send_response(e.code)
            for k, v in self._cors().items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
        except Exception as e:
            self._json(502, {"error": str(e)})

    # ── /api/generate ────────────────────────────────────────
    def do_POST(self):
        if self.path != "/api/generate":
            self._json(404, {"error": f"Unknown route: {self.path}"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None
        try:
            req_data = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            self._json(400, {"error": "Invalid JSON body"})
            return

        api_key   = req_data.get("api_key", "")
        voice_id  = req_data.get("voice_id", "")
        words_raw = req_data.get("words", req_data.get("text", ""))
        speed     = float(req_data.get("speed", 0.85))

        if not api_key:
            self._json(400, {"error": "Missing api_key"})
            return
        if not voice_id:
            self._json(400, {"error": "Missing voice_id"})
            return
        if not str(words_raw).strip():
            self._json(400, {"error": "Missing words — enter some words first."})
            return

        # Split input into individual word lines
        raw_words = re.split(r'[\n,]+', str(words_raw))
        word_list = [w.strip() for w in raw_words if w.strip()][:20]

        if not word_list:
            self._json(400, {"error": "No valid words found."})
            return

        # Convert any bare numbers in words → word form ("1" → "one")
        spoken_words = [_convert_numbers_in_phrase(w) for w in word_list]

        # Deduplicate consecutive identical entries
        deduped = []
        for w in spoken_words:
            if not deduped or w != deduped[-1]:
                deduped.append(w)
        spoken_words = deduped

        print(f"[server] Input : {word_list}")
        print(f"[server] Spoken: {spoken_words}")

        all_audio = b''
        two_sec   = self._make_silence_mp3(2.0)   # 2-second pause after number
        three_sec = self._make_silence_mp3(3.0)   # 3-second pause between words
        n = len(spoken_words)

        for i, word in enumerate(spoken_words):
            ordinal = _num_words(i + 1)

            # ① Say the number (e.g. "one")
            tts_body = json.dumps({
                "text": ordinal,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                "speed": speed,
            }).encode()
            try:
                audio_bytes = self._proxy_bytes(
                    "POST",
                    f"/v1/text-to-speech/{voice_id}/stream",
                    {"xi-api-key": api_key,
                     "Content-Type": "application/json",
                     "Accept": "audio/mpeg"},
                    tts_body
                )
                all_audio += audio_bytes
                print(f"[server] ✓ [{i+1}/{n}] number '{ordinal}' — {len(audio_bytes)} bytes")
            except Exception as e:
                print(f"[server] ✗ [{i+1}/{n}] number '{ordinal}' failed: {e}")

            # ② 2-second pause after the number
            all_audio += two_sec

            # ③ Say the word 3 times, each followed by a 3-second pause
            for rep in range(3):
                tts_body2 = json.dumps({
                    "text": word,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                    "speed": speed,
                }).encode()
                try:
                    audio_bytes = self._proxy_bytes(
                        "POST",
                        f"/v1/text-to-speech/{voice_id}/stream",
                        {"xi-api-key": api_key,
                         "Content-Type": "application/json",
                         "Accept": "audio/mpeg"},
                        tts_body2
                    )
                    all_audio += audio_bytes
                    print(f"[server] ✓ [{i+1}/{n}] word '{word}' rep {rep+1}/3 — {len(audio_bytes)} bytes")
                except Exception as e:
                    print(f"[server] ✗ [{i+1}/{n}] word '{word}' rep {rep+1}/3 failed: {e}")

                # 3-second pause after each repetition
                all_audio += three_sec

        if not all_audio:
            self._json(500, {"error": "All TTS calls failed."})
            return

        # ── Slow down by 25% (tempo factor 0.75) ─────────────
        print(f"[server] Applying 25% tempo reduction...")
        slowed = self._tempo_shift_mp3(all_audio, factor=0.75)
        print(f"[server] Before: {len(all_audio):,} bytes → After: {len(slowed):,} bytes")

        b64 = base64.b64encode(slowed).decode()
        print(f"[server] Done — {len(slowed):,} bytes, {n} words")
        self._json(200, {
            "ok": True,
            "word_count": n,
            "download_url": f"data:audio/mpeg;base64,{b64}",
            "size_bytes": len(slowed),
        })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"\n🎙️ WordMP3 Server on http://127.0.0.1:{port}")
    print(f"   GET  /api/voices?api_key=<KEY>")
    print(f"   POST /api/generate\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()
