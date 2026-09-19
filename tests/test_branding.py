"""One identity everywhere: the logo drives portal, splash and boot menu."""
import re
import struct
from pathlib import Path

ROOT = Path(__file__).parents[1]
BRANDING = ROOT / 'branding'


def png_size(path):
    header = path.read_bytes()[:24]
    assert header[:8] == b'\x89PNG\r\n\x1a\n', path
    return struct.unpack('>II', header[16:24])


def test_every_derived_asset_exists_and_is_a_png():
    for name in ('AIOS_Logo.png', 'logo-mark.png', 'logo-lockup.png', 'splash-logo.png',
                 'grub-background.png', 'favicon.png', 'dot.png'):
        width, height = png_size(BRANDING / name)
        assert width > 0 and height > 0


def test_the_boot_menu_picture_fills_the_screen():
    assert png_size(BRANDING / 'grub-background.png') == (1024, 768)


def test_the_portal_ships_the_logo_and_uses_it():
    public = ROOT / 'frontend-admin/public'
    assert png_size(public / 'aios-mark.png') and png_size(public / 'aios-lockup.png')
    app = (ROOT / 'frontend-admin/src/App.tsx').read_text()
    assert '/admin/aios-mark.png' in app and '/admin/aios-lockup.png' in app
    assert (ROOT / 'frontend-admin/index.html').read_text().count('/admin/favicon.png') == 1


def test_the_palette_comes_from_the_logo():
    css = (ROOT / 'frontend-admin/src/style.css').read_text()
    for colour in ('#22d3ee', '#3b6dff', '#8b5cf6'):
        assert colour in css, colour
    # No leftovers of the previous green/light theme.
    assert '#17796d' not in css and '#f4f6f7' not in css


def test_the_splash_and_the_boot_menu_are_built_from_the_same_assets():
    image = (ROOT / 'scripts/build-image.sh').read_text()
    assert 'branding/splash-logo.png' in image and 'branding/dot.png' in image
    iso = (ROOT / 'scripts/build-iso.sh').read_text()
    assert 'branding/grub-background.png' in iso and 'branding/grub-theme.txt' in iso


def test_the_chat_keeps_its_own_name_and_logo():
    """Open WebUI's licence forbids replacing its branding; only colours change."""
    css = (ROOT / 'config/webui-custom.css').read_text()
    assert '--color-gray-900' in css and '#22d3ee' in css
    assert not re.search(r'logo|favicon|splash|Open WebUI["\']', css, re.I) or 'licence' in css
    image = (ROOT / 'scripts/build-image.sh').read_text()
    assert 'config/webui-custom.css' in image
    assert 'static/logo.png' not in image and 'static/favicon' not in image
