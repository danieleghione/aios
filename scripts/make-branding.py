"""Derive every AIOS boot and portal image from branding/AIOS_Logo.png.

Run inside the image rootfs, which has Pillow:
  sudo cp branding/AIOS_Logo.png build/rootfs/tmp/ && \
  sudo chroot build/rootfs /opt/aios/webui/bin/python /tmp/make-branding.py /tmp/out
The results are committed under branding/, so a build needs no image library.
"""
import sys
from pathlib import Path
from PIL import Image

SOURCE = Path('/tmp/AIOS_Logo.png')
out = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/out')
out.mkdir(parents=True, exist_ok=True)
logo = Image.open(SOURCE).convert('RGB')
W, H = logo.size

# The artwork sits on black with a glow. Luminance becomes alpha, so the mark
# keeps its halo and drops onto any dark surface without a black box.
alpha = logo.convert('L').point(lambda v: min(255, int((v / 255) ** 0.75 * 255)))
art = logo.convert('RGBA')
art.putalpha(alpha)

# Bands measured from the source: the A mark, the AIOS wordmark, the tagline
# and the claim line.
MARK = (240, 110, 990, 700)
LOCKUP = (160, 110, 1080, 975)
FULL = (160, 110, 1080, 1120)


def fit(image, width=None, height=None):
    if width:
        height = round(image.height * width / image.width)
    else:
        width = round(image.width * height / image.height)
    return image.resize((width, height), Image.LANCZOS)


def save(image, name):
    image.save(out / name)
    print(name, image.size)


mark = art.crop(MARK)
save(fit(mark, width=512), 'logo-mark.png')          # portal header, favicon source
save(fit(mark, width=192), 'favicon.png')
save(fit(art.crop(LOCKUP), width=900), 'logo-lockup.png')   # login page, splash
save(fit(art.crop(FULL), width=1000), 'logo-full.png')      # documentation, wide uses

# Boot splash: the lockup on the logo's own black, centred in the upper half.
splash = fit(art.crop(LOCKUP), width=760)
save(splash, 'splash-logo.png')
menu_logo = fit(art.crop(LOCKUP), width=430)
screen = Image.new('RGB', (1024, 768), (0, 0, 0))
screen.paste(menu_logo, ((1024 - menu_logo.width) // 2, 70), menu_logo)
save(screen, 'grub-background.png')

# Progress dot in the brand cyan, matching the top of the A.
dot = Image.new('RGBA', (16, 16), (0, 0, 0, 0))
from PIL import ImageDraw
ImageDraw.Draw(dot).ellipse((0, 0, 15, 15), fill=(34, 211, 238, 255))
save(dot, 'dot.png')
