#!/usr/bin/env python3
"""EW promo video worker — branded social videos for bids.

Two modes (promo_video_jobs.kind):
  'photo' — seeded from a clicked bid photo
  'spec'  — no photo needed; Veo text-to-video from the bid's specs
            (year/make/model/trim/color + canon VIN-decode fields)

Both modes render a ~30s dealership walkaround: exterior approach -> side/rear
-> interior dash -> cabin/seats. No driving shots. Scenes are chained via
last-frame seeding so it stays the same car throughout. A female-voiced
narration (Gemini TTS, voice 'Leda') written from the specs is mixed over the
scene audio. Optional price card + EW watermark burned in, per-bid OG landing
page emitted, share URL stored on the job row. notify='telegram' sends the
link via the existing EW Telegram bot (creds parsed at runtime from
/usr/local/bin/vauto_cookie_alert.py — not duplicated here).

Runs on C1 under the expwholesale venv (systemd: ew-promo-video.service).
"""
import os
import re
import subprocess
import sys
import time
import traceback

import psycopg2
import psycopg2.extras
import requests

import google.genai as genai
from google.genai import types

GEMINI_ENV = "/etc/ew-gemini.env"
APP_ROOT = "/opt/expwholesale"
PROMO_ROOT = os.path.join(APP_ROOT, "static", "uploads", "promo")
PUBLIC_BASE = "https://experience-wholesale.net/static/uploads/promo"
TG_SOURCE = "/usr/local/bin/vauto_cookie_alert.py"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

SCENE_SECONDS = 8
XFADE = 0.5
TTS_MODEL = "gemini-2.5-flash-preview-tts"
TTS_VOICE = "Leda"  # female
SCRIPT_MODEL = "gemini-2.5-flash"

VEO_MODELS = [
    ("veo-3.1-fast-generate-preview", {"resolution": "1080p"}),
    ("veo-3.1-generate-preview", {"resolution": "1080p"}),
    ("veo-3.0-generate-001", {"resolution": "1080p"}),
    ("veo-2.0-generate-001", {}),
]
# quality='max' — standard Veo 3.1 first (~$0.40/s vs ~$0.15/s fast)
VEO_MODELS_MAX = [
    ("veo-3.1-generate-preview", {"resolution": "1080p"}),
    ("veo-3.1-fast-generate-preview", {"resolution": "1080p"}),
    ("veo-3.0-generate-001", {"resolution": "1080p"}),
    ("veo-2.0-generate-001", {}),
]


def _ew_service_env():
    """Read DATABASE_URL / Twilio creds from the live expwholesale unit so no
    secret is duplicated into this file or the worker's own unit."""
    import shlex
    out = subprocess.run(["systemctl", "show", "expwholesale", "-p", "Environment", "--value"],
                         capture_output=True, text=True).stdout.strip()
    env = {}
    for tok in shlex.split(out):
        if "=" in tok:
            k, v = tok.split("=", 1)
            env[k] = v
    return env


_EW_ENV = {}
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
if not (DATABASE_URL and TWILIO_SID):
    _EW_ENV = _ew_service_env()
    DATABASE_URL = DATABASE_URL or _EW_ENV.get("DATABASE_URL", "")
    TWILIO_SID = TWILIO_SID or _EW_ENV.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_TOKEN = TWILIO_TOKEN or _EW_ENV.get("TWILIO_AUTH_TOKEN", "")

LANDING_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Experience Wholesale</title>
<meta property="og:type" content="video.other">
<meta property="og:title" content="{title} — Experience Wholesale">
<meta property="og:description" content="{desc}">
<meta property="og:site_name" content="Experience Wholesale">
<meta property="og:url" content="{page_url}">
<meta property="og:image" content="{thumb_url}">
<meta property="og:image:width" content="1920">
<meta property="og:image:height" content="1080">
<meta property="og:video" content="{video_url}">
<meta property="og:video:type" content="video/mp4">
<meta property="og:video:width" content="1920">
<meta property="og:video:height" content="1080">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title} — Experience Wholesale">
<meta name="twitter:image" content="{thumb_url}">
<style>
  html, body {{ margin: 0; height: 100%; background: #0b0b0d; color: #f5f5f5;
               font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  .wrap {{ min-height: 100%; display: flex; flex-direction: column; align-items: center;
          justify-content: center; box-sizing: border-box; padding: 8px 0 20px; }}
  .player {{ position: relative; width: 100%; max-width: 980px; }}
  video {{ display: block; width: 100%; height: auto; background: #000; }}
  @media (min-width: 700px) {{ video {{ border-radius: 14px; box-shadow: 0 12px 50px rgba(200,30,40,.35); }} }}
  #soundBtn {{
    position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%);
    padding: 10px 22px; border: 0; border-radius: 999px; cursor: pointer;
    background: rgba(0,0,0,.65); color: #fff; font-size: 1rem; font-weight: 600;
    backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
  }}
  h1 {{ font-size: 1.2rem; font-weight: 600; margin: 16px 12px 4px; text-align: center; }}
  p  {{ margin: 0; color: #9a9aa2; font-size: .85rem; text-align: center; }}
  a  {{ color: #e04444; text-decoration: none; font-weight: 600; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="player">
    <video id="v" src="{video_path}" poster="{thumb_path}"
           muted autoplay playsinline loop preload="auto"></video>
    <button id="soundBtn">&#128266; Tap for sound</button>
  </div>
  <h1>{title}</h1>
  <p>{sub} &bull; <a href="https://experience-wholesale.net">Experience Wholesale</a></p>
</div>
<script>
  var v = document.getElementById('v');
  var btn = document.getElementById('soundBtn');
  function enableSound() {{
    v.muted = false; v.volume = 1; v.controls = true; btn.remove();
    if (v.paused) v.play();
  }}
  btn.addEventListener('click', function (e) {{ e.stopPropagation(); enableSound(); }});
  v.addEventListener('click', enableSound, {{ once: true }});
  var p = v.play();
  if (p && p.catch) {{
    p.catch(function () {{
      btn.innerHTML = '&#9654;&#65039; Play';
      btn.addEventListener('click', function () {{ v.play(); }}, {{ once: true }});
    }});
  }}
</script>
</body>
</html>
"""


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_gemini_key():
    with open(GEMINI_ENV) as f:
        for line in f:
            line = line.strip()
            if "=" in line and "KEY" in line.split("=")[0].upper():
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"no key in {GEMINI_ENV}")


def load_telegram():
    """Reuse the EW Telegram bot: env first, else the literal defaults baked
    into the existing alert script (token = NNN:base64ish, chat = digits)."""
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if bot and chat:
        return bot, chat
    try:
        content = open(TG_SOURCE).read()
        m = re.search(r"['\"](\d{6,}:[A-Za-z0-9_-]{30,})['\"]", content)
        if m:
            bot = bot or m.group(1)
        m = re.search(r"TELEGRAM_CHAT_ID['\"]\s*,\s*['\"](-?\d+)['\"]", content)
        if m:
            chat = chat or m.group(1)
    except OSError:
        pass
    return bot, chat


def telegram_send(text):
    bot, chat = load_telegram()
    if not (bot and chat):
        log("  telegram: creds not found, skipping notify")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                          timeout=15)
        log(f"  telegram: sendMessage {r.status_code}")
    except Exception as e:
        log(f"  telegram: failed {e}")


def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def set_progress(conn, job_id, text):
    """Live step indicator for the /promo-videos page. Commits immediately so
    no transaction is ever held open during generation."""
    try:
        cur = conn.cursor()
        cur.execute("UPDATE promo_video_jobs SET progress=%s, updated_at=now() WHERE id=%s",
                    (text, job_id))
        conn.commit()
    except Exception as e:
        log(f"  progress update failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def fetch_photo_bytes(photo):
    lp = photo.get("local_path")
    if lp:
        path = lp if os.path.isabs(lp) else os.path.join(APP_ROOT, lp.lstrip("/"))
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    url = photo["url"]
    if url.startswith("/static/"):
        path = os.path.join(APP_ROOT, url.lstrip("/"))
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    auth = (TWILIO_SID, TWILIO_TOKEN) if "twilio" in url else None
    resp = requests.get(url, timeout=30, auth=auth, headers={"User-Agent": UA})
    resp.raise_for_status()
    return resp.content


def car_label(bid):
    bits = [str(bid.get("year") or "").strip(), (bid.get("make") or "").strip().title(),
            (bid.get("model") or "").strip()]
    label = " ".join(b for b in bits if b)
    return label or "this vehicle"


def car_description(bid):
    """Long-form car description for Veo prompts, from bid + canon decode fields."""
    color = (bid.get("color") or "").strip().lower()
    trim = (bid.get("trim") or "").strip()
    body = (bid.get("canon_body_class") or "").strip().lower()
    bits = [color, str(bid.get("year") or ""), (bid.get("make") or "").title(),
            bid.get("model") or "", trim]
    desc = " ".join(b for b in bits if b).strip() or "car"
    if body and body not in desc.lower():
        desc += f" ({body})"
    return desc


def describe_photo_look(client, image_bytes, mime):
    """Ask Gemini vision for the car's actual appearance so prompts/narration
    mimic the real unit (DB color fields are often empty)."""
    try:
        resp = client.models.generate_content(
            model=SCRIPT_MODEL,
            contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime),
                      "If this image is NOT a photograph of an actual car (e.g. a paper "
                      "document, VIN sheet, window sticker, text screenshot), reply exactly "
                      "NOT_A_CAR. Otherwise, in 10 words or fewer, the exterior paint color "
                      "and wheel finish of the car, e.g. 'silver flare metallic with gloss "
                      "black wheels'. No other text."])
        look = (resp.text or "").strip().strip(".").lower()
        if "not_a_car" in look:
            return "NOT_A_CAR"
        return look if 0 < len(look) < 90 else None
    except Exception as e:
        log(f"  vision look failed: {e}")
        return None


def build_scene_prompts(bid, n_scenes=4, look=None, interior_color=None):
    car = car_description(bid)
    if look:
        car = f"{look} {car_label(bid)} {bid.get('trim') or ''}".strip()
    int_col = (interior_color or bid.get("int_color") or "").strip().lower()
    cabin = f"{int_col} interior" if int_col else "interior"
    style = ("Professional dealership walkaround video, photorealistic, smooth handheld "
             "gimbal, clean modern dealership delivery area, soft even lighting, no people, "
             "no text, no driving — the car stays parked. Audio: quiet showroom ambience "
             "only — absolutely no speech, no narration, no voices, no music.")
    exterior = [
        f"{style} The camera slowly walks toward the front three-quarter view of a parked {car}, "
        "starting wide and moving closer, light gliding across the bodywork. Subtle showroom ambience.",
        f"{style} Continuing the same shot, the camera moves low and close along the front of the {car}, "
        "lingering on the headlights, grille and front wheel, paint reflections sweeping past.",
        f"{style} Continuing the same shot, the camera keeps walking slowly around the side of the {car} "
        "to the rear three-quarter view, passing the wheels and door lines, taillights coming into frame.",
        f"{style} Continuing the same shot, the camera sweeps slowly across the rear of the {car}, "
        "taillights, badges and exhaust in crisp detail, then rises slightly over the rear deck.",
    ]
    interior = [
        f"{style} The camera moves toward the driver's door of the {car} as it opens, and glides inside "
        f"to reveal the {cabin} — dashboard, steering wheel and infotainment screen in crisp detail.",
        f"{style} Inside the {car}, the camera pans slowly across the {cabin}, seats and center console, "
        "then settles on a clean final view of the cabin.",
    ]
    n = max(2, min(n_scenes, 6))
    n_ext = n - 2
    picks = {0: [], 1: [exterior[0]], 2: [exterior[0], exterior[2]],
             3: [exterior[0], exterior[2], exterior[3]], 4: exterior}[min(n_ext, 4)]
    return picks + interior


def veo_generate(client, prompt, out_path, image_bytes=None, mime="image/jpeg", models=None):
    kwargs = {}
    if image_bytes:
        kwargs["image"] = types.Image(image_bytes=image_bytes, mime_type=mime)
    operation = None
    last_err = None
    for model, extra in (models or VEO_MODELS):
        config = types.GenerateVideosConfig(aspect_ratio="16:9",
                                            duration_seconds=SCENE_SECONDS, **extra)
        try:
            log(f"  veo: trying {model}")
            operation = client.models.generate_videos(model=model, prompt=prompt,
                                                      config=config, **kwargs)
            log(f"  veo: accepted by {model}")
            break
        except Exception as e:
            last_err = e
            log(f"  veo: {model} rejected: {str(e)[:200]}")
    if operation is None:
        raise RuntimeError(f"all Veo models rejected: {last_err}")

    start = time.time()
    while not operation.done:
        time.sleep(15)
        operation = client.operations.get(operation)
        if time.time() - start > 900:
            raise RuntimeError("Veo polling timed out after 15 min")
    if operation.error:
        raise RuntimeError(f"Veo failed: {operation.error}")
    videos = operation.response.generated_videos
    if not videos:
        raise RuntimeError(f"Veo returned no video (filtered?): {operation.response}")
    video = videos[0].video
    data = video.video_bytes or client.files.download(file=video)
    with open(out_path, "wb") as f:
        f.write(data)
    log(f"  veo: saved {out_path} ({len(data)/1e6:.1f} MB in {int(time.time()-start)}s)")


def veo_generate_with_retry(client, prompt, out_path, image_bytes=None,
                            mime="image/jpeg", tries=3, models=None):
    """Google intermittently 500s mid-generation (code 13, 'try again in a few
    minutes') — retry the whole scene a couple of times before giving up."""
    for attempt in range(1, tries + 1):
        try:
            return veo_generate(client, prompt, out_path, image_bytes=image_bytes,
                                mime=mime, models=models)
        except RuntimeError as e:
            transient = "'code': 13" in str(e) or "internal server" in str(e).lower()
            if attempt == tries or not transient:
                raise
            log(f"  veo: transient failure (attempt {attempt}/{tries}), waiting 90s: {str(e)[:120]}")
            time.sleep(90)


def write_voiceover(client, bid, price, out_wav, msrp=None, look=None, duration=30,
                    custom_script=None):
    """Gemini writes a duration-matched narration from the specs (or the operator
    supplies the exact script); Gemini TTS reads it (female)."""
    if custom_script:
        script = re.sub(r'["\*]', "", custom_script.strip())
        log(f"  voiceover script (operator-supplied): {script[:120]}...")
    else:
        miles = bid.get("mileage")
        facts = car_label(bid)
        if (bid.get("trim") or "").strip():
            facts += f" {bid['trim'].strip()}"
        if look:
            facts += f", finished in {look}"
        elif (bid.get("color") or "").strip():
            facts += f", {bid['color'].strip().lower()}"
        if miles:
            facts += f", {int(miles):,} miles"
        if msrp:
            facts += f", original window-sticker MSRP {msrp}"
        if price:
            facts += f", offered at {price}"
        words = int(duration * 2.4)
        ask = (f"Write a warm, confident {words-10}-{words} word dealership promo narration for "
               f"this vehicle, to be read by a female voice over a {duration}-second walkaround "
               "video. Plain sentences, no stage directions, no emojis, no quotes. "
               "Begin with exactly: 'Welcome to Experience Wholesale.' Mention the key specs "
               "naturally — if an original MSRP is given, mention it as what this car stickered "
               "for new. End with a short invitation to ask about this car. "
               f"Vehicle: {facts}")
        script = client.models.generate_content(model=SCRIPT_MODEL, contents=ask).text.strip()
        script = re.sub(r'["\*]', "", script)
        log(f"  voiceover script: {script[:120]}...")

    resp = client.models.generate_content(
        model=TTS_MODEL, contents=script,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE)))))
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    raw = out_wav + ".pcm"
    with open(raw, "wb") as f:
        f.write(pcm)
    ffmpeg_run(["-f", "s16le", "-ar", "24000", "-ac", "1", "-i", raw, out_wav])
    os.remove(raw)
    log(f"  voiceover: {out_wav} ({os.path.getsize(out_wav)/1e3:.0f} KB)")


def ffprobe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def ffmpeg_run(args, expect_duration=None):
    """Run ffmpeg at idle priority (this box also serves the live dashboard) and
    VERIFY the output (exists, sized, and full duration when known).

    Long in-worker encodes have repeatedly exited rc=0 with a truncated,
    moov-less file (cause still unidentified — happens only inside this worker
    process; shell and systemd-run replays always succeed). So: validate hard,
    retry, and on the 3rd attempt run ffmpeg as a detached transient systemd
    unit, which has never exhibited the failure."""
    base = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args]
    out_path = args[-1]
    proc = None
    for attempt in (1, 2, 3):
        if attempt < 3:
            cmd = ["nice", "-n", "19", "ionice", "-c", "3", *base]
        else:
            cmd = ["systemd-run", "--wait", "--collect", "--quiet", "-p", "Nice=19",
                   "/usr/bin/ffmpeg", *base[1:]]
        proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        dur = ffprobe_duration(out_path) if (proc.returncode == 0 and size > 1024
                                             and expect_duration) else None
        ok = (proc.returncode == 0 and size > 1024
              and (expect_duration is None or (dur or 0) >= expect_duration - 1.5))
        if ok:
            if attempt > 1:
                log(f"  ffmpeg recovered on attempt {attempt}")
            return
        log(f"  ffmpeg attempt {attempt} bad (rc={proc.returncode}, size={size}, "
            f"dur={dur}): {proc.stderr[-300:]}")
        time.sleep(3)
    raise RuntimeError(f"ffmpeg failed 3x: rc={proc.returncode}: {proc.stderr[-800:]}")


def format_price(price_raw):
    digits = re.sub(r"[^\d]", "", price_raw or "")
    if not digits:
        return None
    return "${:,}".format(int(digits))


def ffmpeg_escape(text):
    return text.replace("\\", "").replace("'", "").replace(":", r"\:").replace(",", r"\,")


def finalize(scenes, voice_wav, price, out_path):
    """Chain N scenes with crossfades, mix the voiceover over ducked scene audio,
    stamp watermark + optional price card."""
    n = len(scenes)
    inputs = []
    for s in scenes:
        inputs += ["-i", s]
    if voice_wav:
        inputs += ["-i", voice_wav]

    fc = []
    # video chain
    prev = "0:v"
    for i in range(1, n):
        offset = i * (SCENE_SECONDS - XFADE)
        outl = f"x{i}" if i < n - 1 else "xv"
        fc.append(f"[{prev}][{i}:v]xfade=transition=fade:duration={XFADE}:offset={offset}[{outl}]")
        prev = outl
    # audio chain (scene ambience)
    aprev = "0:a"
    for i in range(1, n):
        outl = f"a{i}" if i < n - 1 else "ax"
        fc.append(f"[{aprev}][{i}:a]acrossfade=d={XFADE}[{outl}]")
        aprev = outl
    if voice_wav:
        # narration is the ONLY audible track — Veo's scene audio sometimes
        # contains invented speech that bleeds through under the voiceover
        fc.append(f"[ax]volume=0.0[bg]")
        fc.append(f"[{n}:a]adelay=800|800,apad[vo]")
        fc.append("[bg][vo]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        fc.append("[ax]anull[a]")

    filters = [
        f"drawtext=fontfile={FONT_BOLD}:text='EXPERIENCE WHOLESALE':fontcolor=white@0.85:"
        "fontsize=38:x=w-tw-40:y=h-th-64:shadowcolor=black@0.6:shadowx=2:shadowy=2",
        f"drawtext=fontfile={FONT_REG}:text='experience-wholesale.net':fontcolor=white@0.7:"
        "fontsize=24:x=w-tw-40:y=h-th-28:shadowcolor=black@0.6:shadowx=2:shadowy=2",
    ]
    if price:
        price_txt = ffmpeg_escape(price)
        filters.append(
            f"drawtext=fontfile={FONT_BOLD}:text='{price_txt}':fontcolor=white:fontsize=72:"
            "x=40:y=h-th-78:box=1:boxcolor=black@0.55:boxborderw=18")
        filters.append(
            f"drawtext=fontfile={FONT_REG}:text='ASK ABOUT THIS CAR':fontcolor=white@0.85:fontsize=26:"
            "x=44:y=h-th-32:box=1:boxcolor=black@0.55:boxborderw=10")
    fc.append(f"[xv]{','.join(filters)}[v]")

    expected = n * (SCENE_SECONDS - XFADE) + XFADE
    # +faststart puts the moov index at the file head so phones start playing
    # immediately; maxrate keeps 1080p streamable on cellular
    ffmpeg_run([*inputs, "-filter_complex", ";".join(fc),
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-threads", "8",
                "-maxrate", "8M", "-bufsize", "16M",
                "-movflags", "+faststart",
                "-c:a", "aac", "-b:a", "160k", out_path],
               expect_duration=expected)


def extract_last_frame(video_path, out_jpg):
    ffmpeg_run(["-sseof", "-0.25", "-i", video_path, "-frames:v", "1", "-q:v", "2", out_jpg])


def process_job(client, conn, job):
    bid_id = job["bid_id"]
    cur = conn.cursor()
    cur.execute("SELECT id, vin, year, make, model, trim, color, int_color, mileage, "
                "canon_body_class, canon_drive_type FROM bids WHERE id=%s", (bid_id,))
    bid = cur.fetchone()
    if not bid:
        raise RuntimeError(f"bid {bid_id} not found")

    # window-sticker data (iPacket) — actual MSRP + factory colors when present
    cur.execute("SELECT total_msrp, exterior_color, interior_color, raw_json::text AS raw "
                "FROM ipacket_lookups WHERE (bid_id=%s OR vin=%s) AND total_msrp IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (bid_id, bid.get("vin")))
    sticker = dict(cur.fetchone() or {})
    # the structured color columns are often empty but the raw sticker text
    # carries "EXTERIOR: ... INTERIOR: ..." — parse them out
    raw = sticker.get("raw") or ""
    if raw and not (sticker.get("exterior_color") or "").strip():
        m = re.search(r"EXTERIOR:\s*(.{3,45}?)\s*(?:INTERIOR:|\\n\\u2022|\\u2022|•)", raw, re.S)
        if m:
            sticker["exterior_color"] = re.sub(r"(\\n|\s)+", " ", m.group(1)).strip().title()
            log(f"  sticker exterior color (raw parse): {sticker['exterior_color']}")
    if raw and not (sticker.get("interior_color") or "").strip():
        m = re.search(r"INTERIOR:\s*(.{3,45}?)\s*(?:\\n\\u2022|\\u2022|•|\\n[A-Z])", raw, re.S)
        if m:
            sticker["interior_color"] = re.sub(r"(\\n|\s)+", " ", m.group(1)).strip().title()
            log(f"  sticker interior color (raw parse): {sticker['interior_color']}")
    msrp = format_price(str(sticker.get("total_msrp") or "")) if sticker.get("total_msrp") else None

    kind = (job.get("kind") or "photo").strip()
    photo = None
    if kind != "spec" and job.get("photo_id"):
        cur.execute("SELECT * FROM bid_photos WHERE id=%s AND bid_id=%s",
                    (job["photo_id"], bid_id))
        photo = cur.fetchone()
        if not photo:
            raise RuntimeError(f"photo {job['photo_id']} not found on bid {bid_id}")

    # CRITICAL: all DB reads are done — release the transaction NOW. Holding an
    # idle-in-transaction connection through the ~10min generation phase queues
    # EW's runtime ALTER TABLEs behind our AccessShare lock, and the whole
    # dashboard queues behind the ALTER (froze the site 4x on 2026-06-12).
    conn.commit()

    seed_bytes = None
    seed_mime = "image/jpeg"
    if photo:
        seed_bytes = fetch_photo_bytes(photo)
        seed_mime = "image/png" if seed_bytes[:4] == b"\x89PNG" else "image/jpeg"
    elif kind != "spec":
        kind = "spec"  # photo job without a photo falls back to spec mode

    # mimic the real car: factory sticker color first, else read it off the photo —
    # but only if the "photo" is actually a car (bids often carry VIN-sheet scans)
    look = (sticker.get("exterior_color") or "").strip().lower() or None
    if seed_bytes:
        photo_look = describe_photo_look(client, seed_bytes, seed_mime)
        if photo_look == "NOT_A_CAR":
            log("  photo is a document/screenshot, not a car — using spec mode, no seed")
            seed_bytes = None
            kind = "spec"
        elif photo_look and not look:
            look = photo_look
            log(f"  look from photo: {look}")

    # build OUTSIDE static/uploads (a sync sweeps that tree every minute and
    # in-flight build files there have been truncated/vanished), publish by move
    out_dir = os.path.join(PROMO_ROOT, f"bid{bid_id}")
    os.makedirs(out_dir, exist_ok=True)
    j = job["id"]
    work_dir = f"/opt/ew-promo-work/bid{bid_id}_j{j}"
    os.makedirs(work_dir, exist_ok=True)
    final = os.path.join(work_dir, f"j{j}.mp4")
    thumb = os.path.join(work_dir, f"j{j}.jpg")
    page = os.path.join(out_dir, f"j{j}.html")
    voice_wav = os.path.join(work_dir, f"j{j}_voice.wav")

    quality = (job.get("quality") or "fast").strip()
    n_scenes = int(job.get("scenes") or 4)
    prompts = build_scene_prompts(bid, n_scenes=n_scenes, look=look,
                                  interior_color=(sticker.get("interior_color") or "").strip() or None)
    scenes = []
    prev_frame = seed_bytes
    prev_mime = seed_mime
    for i, prompt in enumerate(prompts, 1):
        scene = os.path.join(work_dir, f"j{j}_scene{i}.mp4")
        log(f"job {j}: scene {i}/{len(prompts)} ({kind}, {quality})")
        set_progress(conn, j, f"scene {i}/{len(prompts)}")
        veo_generate_with_retry(client, prompt, scene, image_bytes=prev_frame, mime=prev_mime,
                                models=VEO_MODELS_MAX if quality == "max" else None)
        scenes.append(scene)
        bridge = os.path.join(work_dir, f"j{j}_bridge{i}.jpg")
        extract_last_frame(scene, bridge)
        with open(bridge, "rb") as f:
            prev_frame = f.read()
        prev_mime = "image/jpeg"

    price = format_price(job.get("price"))
    voice = (job.get("voice") or "female").strip()
    set_progress(conn, j, "scenes done")
    if voice and voice != "none":
        set_progress(conn, j, "voiceover")
        log(f"job {j}: voiceover ({voice}, msrp={msrp})")
        duration = int(len(prompts) * (SCENE_SECONDS - XFADE))
        write_voiceover(client, bid, price, voice_wav, msrp=msrp, look=look, duration=duration,
                        custom_script=(job.get("script") or "").strip() or None)
    else:
        voice_wav = None

    log(f"job {j}: finalize (price card: {price})")
    set_progress(conn, j, "stitching + price card")
    finalize(scenes, voice_wav, price, final)
    ffmpeg_run(["-ss", "14", "-i", final, "-frames:v", "1", "-q:v", "2", thumb])

    # publish: move finished assets into the served directory
    import shutil
    pub_final = os.path.join(out_dir, f"j{j}.mp4")
    pub_thumb = os.path.join(out_dir, f"j{j}.jpg")
    shutil.move(final, pub_final)
    shutil.move(thumb, pub_thumb)

    title = car_label(bid)
    sub_bits = [b for b in [(bid.get("trim") or "").strip(),
                            (bid.get("color") or "").strip().title() or
                            (sticker.get("exterior_color") or "").strip()] if b]
    if price:
        sub_bits.append(price)
    sub = " • ".join(sub_bits) if sub_bits else "Wholesale"
    desc = f"{sub} — Fleet acquisition & wholesale, experience-wholesale.net"
    base = f"{PUBLIC_BASE}/bid{bid_id}"
    with open(page, "w") as f:
        f.write(LANDING_TEMPLATE.format(
            title=title, desc=desc, sub=sub,
            page_url=f"{base}/j{j}.html",
            video_url=f"{base}/j{j}.mp4", thumb_url=f"{base}/j{j}.jpg",
            video_path=f"j{j}.mp4", thumb_path=f"j{j}.jpg"))

    shutil.rmtree(work_dir, ignore_errors=True)

    url = f"{base}/j{j}.html"
    if (job.get("notify") or "").strip() == "telegram":
        telegram_send(f"🎬 Promo video ready: <b>{title}</b>"
                      + (f" — {price}" if price else "") + f"\n{url}")
    return url


def main():
    client = genai.Client(api_key=load_gemini_key())
    log("ew-promo-video worker started (v2: walkaround/spec/voiceover/telegram)")
    # single-worker design: any job still 'processing' at startup was orphaned
    # by a restart mid-render — requeue it so it isn't stuck forever
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE promo_video_jobs SET status='queued', updated_at=now() "
                    "WHERE status='processing' RETURNING id")
        orphans = [r["id"] for r in cur.fetchall()]
        conn.commit()
        conn.close()
        if orphans:
            log(f"requeued orphaned jobs from previous run: {orphans}")
    except Exception as e:
        log(f"orphan requeue failed: {e}")
    while True:
        conn = None
        try:
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM promo_video_jobs WHERE status='queued' "
                        "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED")
            job = cur.fetchone()
            if not job:
                conn.close()
                time.sleep(5)
                continue
            cur.execute("UPDATE promo_video_jobs SET status='processing', updated_at=now() "
                        "WHERE id=%s", (job["id"],))
            conn.commit()
            try:
                url = process_job(client, conn, job)
                cur.execute("UPDATE promo_video_jobs SET status='done', url=%s, updated_at=now() "
                            "WHERE id=%s", (url, job["id"]))
                conn.commit()
                log(f"job {job['id']}: DONE {url}")
            except Exception as e:
                conn.rollback()
                cur.execute("UPDATE promo_video_jobs SET status='error', error=%s, updated_at=now() "
                            "WHERE id=%s", (str(e)[:1000], job["id"]))
                conn.commit()
                log(f"job {job['id']}: ERROR {e}\n{traceback.format_exc()}")
            conn.close()
        except Exception as e:
            log(f"worker loop error: {e}")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(10)


if __name__ == "__main__":
    main()
