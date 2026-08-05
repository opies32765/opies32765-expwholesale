#!/usr/bin/env python3
"""make_favicon.py — the DealerPrice icon set, parameterised by brand colour.

    python3 make_favicon.py "#1d4ed8"

dealerprice.net had NO favicon at all (/favicon.ico -> 404, no <link> in the
head), so browsers showed the blank generic page icon — on a site whose whole
job is looking like a business a dealer would trust, and whose outreach is about
to ask 728 of them to bookmark it.

The mark is the site's own logo square: rounded rect, brand colour, white "DP".
Deliberately not a new design — the tab icon should be the thing already at the
top-left of the page.

Colour is an argument because the brand colour is under review (DealerClub's
#226eda sits 32 points away from our #1d4ed8 on one channel). When that lands,
regenerating the whole set is one command rather than a redraw.

Chrome is the renderer — no cairosvg/ImageMagick on this box. NOTE: this
headless build reserves a constant 87px of window height, so everything is
drawn top-left in an oversized window and cropped to the alpha bbox. Centring
in a tight window silently clips the bottom third; that cost two rounds on the
social icons earlier today.

Outputs (Next 14 App Router picks these up automatically, no <link> needed):
    app/favicon.ico    16+32+48 multi-size
    app/icon.png       512  (PWA / high-DPI)
    app/apple-icon.png 180  (iOS home screen)
"""
import io
import os
import subprocess
import sys
import tempfile

from PIL import Image

BRAND = (sys.argv[1] if len(sys.argv) > 1 else '#1d4ed8').strip()
APP = (sys.argv[2] if len(sys.argv) > 2 else '/opt/dealerprice/app')
BOX = 400          # drawn size
WIN = 700          # window: > BOX + Chrome's 87px reservation, with room spare

HTML = """<!doctype html><html><head><style>
  html,body{{margin:0;padding:0;background:transparent}}
  .m{{width:{box}px;height:{box}px;background:{brand};border-radius:{r}px;
      display:flex;align-items:center;justify-content:center;
      font-family:Inter,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
      color:#fff;font-weight:900;font-size:{fs}px;letter-spacing:-{ls}px;
      line-height:1}}
</style></head><body>
<div class="m">DP</div></body></html>"""


def render(path_png):
    tmp = tempfile.mkdtemp()
    html = os.path.join(tmp, 'i.html')
    io.open(html, 'w', encoding='utf-8').write(
        HTML.format(box=BOX, brand=BRAND, r=int(BOX * 0.22),
                    fs=int(BOX * 0.42), ls=int(BOX * 0.02)))
    subprocess.run([
        'google-chrome', '--headless', '--disable-gpu', '--no-sandbox',
        '--hide-scrollbars', '--default-background-color=00000000',
        '--virtual-time-budget=2500', '--window-size=%d,%d' % (WIN, WIN),
        '--screenshot=%s' % path_png, 'file://%s' % html],
        capture_output=True)
    im = Image.open(path_png).convert('RGBA')
    bbox = im.split()[3].getbbox()
    if not bbox:
        raise SystemExit('render produced nothing — check Chrome')
    g = im.crop(bbox)
    if abs(g.width - g.height) > 4:
        raise SystemExit('mark is not square (%dx%d) — clipped again' % g.size)
    return g


def main():
    os.makedirs(APP, exist_ok=True)
    raw = os.path.join(tempfile.mkdtemp(), 'raw.png')
    mark = render(raw)
    print('  rendered %s at %dx%d' % (BRAND, mark.width, mark.height))

    mark.resize((512, 512), Image.LANCZOS).save(os.path.join(APP, 'icon.png'))
    print('  app/icon.png        512x512')

    # iOS composites onto white and ignores transparency, so give it a solid bg
    apple = Image.new('RGBA', (180, 180), (255, 255, 255, 255))
    apple.alpha_composite(mark.resize((180, 180), Image.LANCZOS))
    apple.convert('RGB').save(os.path.join(APP, 'apple-icon.png'))
    print('  app/apple-icon.png  180x180 (opaque — iOS drops alpha)')

    ico = os.path.join(APP, 'favicon.ico')
    mark.resize((48, 48), Image.LANCZOS).save(
        ico, format='ICO', sizes=[(16, 16), (32, 32), (48, 48)])
    print('  app/favicon.ico     16+32+48')


if __name__ == '__main__':
    main()
