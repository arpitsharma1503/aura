"""Wallpaper: choose a picture in Settings → Display (the real file picker), see it behind the
glass on the dashboard, dim it, switch back to AURA light, remove it."""

from pathlib import Path

import pytest

from fixtures import weather_payload

pytestmark = pytest.mark.browser
PHOTO = Path("/usr/share/rpd-wallpaper/coast.jpg")          # Raspberry Pi OS's own wallpaper: a real JPEG


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def settings(chrome, srv):
    p = chrome.new_page()
    p.viewport(1440, 900)
    p.goto(f"{srv.url}/settings#display")
    p.wait_for("!document.getElementById('s-main').hidden", timeout=15)
    return p


@pytest.mark.skipif(not PHOTO.exists(), reason="needs a real photo on this machine")
def test_choose_dim_and_remove_a_wallpaper(chrome, srv, artifacts):
    p = settings(chrome, srv)
    assert p.js("document.querySelector('#display input[name=background][value=image]').disabled") is True   # nothing to show yet
    doc = p.call("DOM.getDocument")["root"]["nodeId"]
    node = p.call("DOM.querySelector", nodeId=doc, selector="#wall-file")["nodeId"]
    p.call("DOM.setFileInputFiles", nodeId=node, files=[str(PHOTO)])
    p.js("document.getElementById('wall-file').dispatchEvent(new Event('change'))")
    p.wait_for("document.getElementById('wall-msg').textContent.startsWith('Done')", timeout=20)
    assert p.js("document.querySelector('#display input[name=background]:checked').value") == "image"
    p.wait_for("document.getElementById('wall-preview').complete && document.getElementById('wall-preview').naturalWidth > 0", timeout=10)
    assert p.js("document.body.dataset.wallpaper") == "image"                   # the Settings page shows it too
    p.screenshot(str(artifacts / "settings-wallpaper.png"))
    # the dashboard shows it behind the glass, and the picture really loads
    d = chrome.new_page()
    d.viewport(1920, 1080)
    d.fixture("/api/weather", weather_payload(0, 9))
    d.goto(f"{srv.url}/")
    d.wait_for("document.body.dataset.wallpaper === 'image' && document.getElementById('weather').dataset.state === 'ok'", timeout=15)
    url = d.js("getComputedStyle(document.querySelector('.ambient')).backgroundImage")
    assert "/api/display/background?v=" in url
    assert d.js(f"fetch({url[5:-2]!r}).then(r => r.status + ' ' + r.headers.get('content-type'))") == "200 image/jpeg"
    d.screenshot(str(artifacts / "dashboard-wallpaper-1920x1080.png"))
    # dim, saved like any setting
    p.js("""(() => { const r = document.querySelector('#display .s-wall__dim'); r.value = '65'; r.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    p.js("document.querySelector('#display .s-save').click()")
    p.wait_for("document.querySelector('#display .s-status').dataset.kind === 'ok'", timeout=10)
    d.wait_for("getComputedStyle(document.body).getPropertyValue('--wall-dim').trim() === '0.65'", timeout=20)
    # back to AURA light (the picture is kept), then remove it
    p.js("document.querySelector('#display input[name=background][value=aura]').click()")   # a real click bubbles
    p.js("document.querySelector('#display .s-save').click()")
    p.wait_for("document.querySelector('#display .s-status').dataset.kind === 'ok'", timeout=10)
    d.wait_for("document.body.dataset.wallpaper === 'aura'", timeout=20)
    assert p.js("!document.getElementById('wall-remove').hidden")
    p.js("document.getElementById('wall-remove').click()")
    p.wait_for("document.getElementById('s-confirm').open")
    p.js("document.getElementById('sc-ok').click()")
    p.wait_for("document.getElementById('wall-msg').textContent === 'Picture removed.'", timeout=10)
    assert p.js("document.getElementById('wall-preview').hidden") is True
    assert p.errors == [] and d.errors == []


def test_a_non_picture_is_refused_politely(chrome, srv, tmp_path):
    bad = tmp_path / "notes.png"
    bad.write_text("not really a picture")
    p = settings(chrome, srv)
    doc = p.call("DOM.getDocument")["root"]["nodeId"]
    node = p.call("DOM.querySelector", nodeId=doc, selector="#wall-file")["nodeId"]
    p.call("DOM.setFileInputFiles", nodeId=node, files=[str(bad)])
    p.js("document.getElementById('wall-file').dispatchEvent(new Event('change'))")
    p.wait_for("document.getElementById('wall-msg').textContent.startsWith('Not uploaded')", timeout=10)
    assert "JPEG, PNG or WebP" in p.text("#wall-msg")
