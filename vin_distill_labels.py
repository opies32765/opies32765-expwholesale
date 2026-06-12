"""Distill Google Vision VIN reads into training labels (2026-06-12). For each of the
785 texted-in intake images, run Vision OCR and keep a 17-char check-digit-VALID VIN.
That gives clean (image -> VIN) pairs — including the door-jamb stickers the 9B fumbles
but Vision reads — to teach the 9B via the multi-task LoRA. Output /tmp/vin_vision_labels.json.
"""
import os, json, glob, re
import app  # for _google_vision_ocr + vin_check_digit_valid (loads shim, fine)
from app import _google_vision_ocr, vin_check_digit_valid

ROOT = "/opt/expwholesale/static/uploads/sms"
files = []
for ext in ("jpg", "jpeg", "png", "webp"):
    files += glob.glob(f"{ROOT}/*/*.{ext}")
files.sort()
print(f"images: {len(files)}", flush=True)

out = []
for i, p in enumerate(files):
    try:
        txt = _google_vision_ocr(open(p, "rb").read()) or ""
        up = txt.upper()
        vin = None
        for m in re.finditer(r"[A-HJ-NPR-Z0-9]{17}", up):
            if vin_check_digit_valid(m.group(0)):
                vin = m.group(0); break
        if not vin:
            for m in re.finditer(r"[A-Z0-9]{17}", up):
                c = m.group(0).replace("O", "0").replace("I", "1").replace("Q", "0")
                if re.match(r"^[A-HJ-NPR-Z0-9]{17}$", c) and vin_check_digit_valid(c):
                    vin = c; break
        if vin:
            out.append({"image": p, "vin": vin})
    except Exception:
        pass
    if (i + 1) % 100 == 0:
        print(f"...{i+1}/{len(files)} labeled={len(out)}", flush=True)

json.dump(out, open("/tmp/vin_vision_labels.json", "w"))
print(f"DONE: {len(out)} VIN labels / {len(files)} images -> /tmp/vin_vision_labels.json", flush=True)
