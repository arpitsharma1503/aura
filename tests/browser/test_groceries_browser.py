"""Grocery feature end to end, through the real UI, in independent browser sessions.

"Phone A" and "phone B" are isolated browser contexts (separate cookies and
storage, like two phones). The entrance display is a third page.
"""

import json
import sqlite3

import pytest

PHONE = (390, 844)


def phone(chrome, server, path="/groceries"):
    p = chrome.new_page()
    p.viewport(*PHONE, mobile=True, scale=2)
    p.goto(f"{server.url}{path}")
    p.wait_for("document.getElementById('g-live').dataset.state === 'live'")
    return p


def display(chrome, server):
    p = chrome.new_page()
    p.viewport(1920, 1080)
    p.goto(f"{server.url}/")
    p.wait_for("document.getElementById('grocery-list').dataset.state !== 'loading'")
    return p


def add_via_ui(p, name, qty="", category=""):
    p.js(f"""(() => {{
        document.getElementById('g-name').focus();
        document.getElementById('g-name').value = {json.dumps(name)};
        document.getElementById('g-qty').value = {json.dumps(qty)};
        document.getElementById('g-cat').value = {json.dumps(category)};
        document.getElementById('g-composer').requestSubmit();
    }})()""")


def open_list_text(p):
    return p.text("#g-groups")


def row_click(p, name, part):
    """Click the check circle ('check') or the name ('body') of a row."""
    p.js(f"""[...document.querySelectorAll('.g-item')].find(li => li.querySelector('.g-name').textContent === {json.dumps(name)})
              .querySelector('.g-{part}').click()""")


def db_items(server):
    con = sqlite3.connect(server.data_dir / "aura.db")
    rows = con.execute("SELECT name, quantity, category, notes, purchased FROM items ORDER BY id").fetchall()
    con.close()
    return rows


def test_full_grocery_flow_with_sync(chrome, server, artifacts):
    a = phone(chrome, server)
    b = phone(chrome, server)
    dash = display(chrome, server)

    # --- empty states
    assert "Your list is empty" in a.text("#g-state")
    assert "Your list is empty" in dash.text("#grocery-list")

    # --- add (name + quantity + category) on phone A -> B and dashboard update live
    add_via_ui(a, "basmati rice", "5 kg", "pantry")
    a.wait_for("document.getElementById('g-groups').textContent.includes('Basmati rice')")
    b.wait_for("document.getElementById('g-groups').textContent.includes('Basmati rice')", timeout=5)
    dash.wait_for("document.getElementById('grocery-list').textContent.includes('Basmati rice')", timeout=5)
    assert "5 kg" in dash.text("#grocery-list")
    assert "Pantry" in a.text("#g-groups")

    # --- quick-add chip (one tap) on phone B
    chip = b.js("(() => { const c = [...document.querySelectorAll('.g-chip')].find(x => x.textContent.includes('Milk')); c.click(); return c.textContent; })()")
    assert "Milk" in chip
    a.wait_for("document.getElementById('g-groups').textContent.includes('Milk')", timeout=5)
    assert "Dairy & eggs" in a.text("#g-groups")             # category guessed

    # --- edit name, quantity, category and notes via the edit sheet on phone A
    row_click(a, "Basmati rice", "body")
    a.wait_for("document.getElementById('g-edit').open")
    a.js("""document.getElementById('e-name').value = 'Brown rice';
            document.getElementById('e-qty').value = '2 kg';
            document.getElementById('e-cat').value = 'pantry';
            document.getElementById('e-notes').value = 'Daawat, 1 kg packs';
            document.getElementById('g-edit-form').requestSubmit();""")
    b.wait_for("document.getElementById('g-groups').textContent.includes('Brown rice')", timeout=5)
    assert "Daawat, 1 kg packs" in b.text("#g-groups") and "2 kg" in b.text("#g-groups")
    dash.wait_for("document.getElementById('grocery-list').textContent.includes('Brown rice')", timeout=5)

    # --- one-tap purchase on phone B -> leaves the dashboard; Undo restores it
    row_click(b, "Milk", "check")
    dash.wait_for("!document.getElementById('grocery-list').textContent.includes('Milk')", timeout=5)
    a.wait_for("document.getElementById('f-purchased').textContent === '1'", timeout=5)
    b.wait_for("!document.getElementById('g-toast').hidden && document.getElementById('g-toast-text').textContent.includes('purchased')")
    b.click("#g-toast-action")                                # Undo
    dash.wait_for("document.getElementById('grocery-list').textContent.includes('Milk')", timeout=5)
    a.wait_for("document.getElementById('f-purchased').textContent === ''", timeout=5)

    # --- purchase again, then filter views on phone A
    row_click(a, "Milk", "check")
    a.wait_for("document.getElementById('f-purchased').textContent === '1'")
    a.click('.g-filter__btn[data-filter="purchased"]')
    a.wait_for("document.getElementById('g-groups').textContent === '' && !document.getElementById('g-done').hidden")
    assert "Milk" in a.text("#g-done-list") and "Brown rice" not in a.text("#g-done-list")
    a.click('.g-filter__btn[data-filter="all"]')
    a.wait_for("document.getElementById('g-groups').textContent.includes('Brown rice') && !document.getElementById('g-done').hidden")
    a.click('.g-filter__btn[data-filter="needed"]')
    a.wait_for("document.getElementById('g-done').hidden && document.getElementById('g-groups').textContent.includes('Brown rice')")
    # undo the purchase from the Purchased view by tapping the filled circle
    a.click('.g-filter__btn[data-filter="purchased"]')
    a.wait_for("document.querySelectorAll('#g-done-list .g-item').length === 1")
    row_click(a, "Milk", "check")
    dash.wait_for("document.getElementById('grocery-list').textContent.includes('Milk')", timeout=5)
    a.wait_for("document.getElementById('g-state').textContent.includes('Nothing purchased yet')")
    a.click('.g-filter__btn[data-filter="needed"]')

    # --- delete needs confirmation: cancel keeps it, confirm removes it everywhere
    row_click(a, "Milk", "body")
    a.wait_for("document.getElementById('g-edit').open")
    a.click("#e-delete")
    a.wait_for("document.getElementById('g-confirm').open")
    assert a.text("#c-title") == "Delete Milk?"
    a.click("#c-cancel")
    a.wait_for("!document.getElementById('g-confirm').open")
    assert ("Milk", None, "dairy", None, 0) in db_items(server)
    a.click("#e-delete")
    a.wait_for("document.getElementById('g-confirm').open")
    a.click("#c-ok")
    dash.wait_for("!document.getElementById('grocery-list').textContent.includes('Milk')", timeout=5)
    b.wait_for("!document.getElementById('g-groups').textContent.includes('Milk')", timeout=5)

    # --- invalid input handled in the UI, nothing saved
    a.js("document.getElementById('g-name').value = '   '; document.getElementById('g-composer').requestSubmit();")
    a.js("document.getElementById('g-name').value = 'x'.repeat(61); document.getElementById('g-composer').requestSubmit();")
    a.wait_for("!document.getElementById('g-form-error').hidden")
    assert "under 60 characters" in a.text("#g-form-error")
    a.js("document.getElementById('g-name').value = 'Bad\\u0007name'; document.getElementById('g-composer').requestSubmit();")
    a.wait_for("document.getElementById('g-toast').classList.contains('is-error')")
    assert "invalid characters" in a.text("#g-toast-text")
    add_via_ui(a, "brown RICE")                                    # duplicate (case-insensitive)
    a.wait_for("document.getElementById('g-toast-text').textContent.includes('already on the list')")
    assert [r[0] for r in db_items(server)] == ["Brown rice"]

    # --- simultaneous edit: both phones open the same item; second save gets a conflict
    row_click(a, "Brown rice", "body")
    row_click(b, "Brown rice", "body")
    a.wait_for("document.getElementById('g-edit').open")
    b.wait_for("document.getElementById('g-edit').open")
    a.js("document.getElementById('e-qty').value = '3 kg'; document.getElementById('g-edit-form').requestSubmit();")
    a.wait_for("!document.getElementById('g-edit').open")
    b.js("document.getElementById('e-qty').value = '10 kg'; document.getElementById('g-edit-form').requestSubmit();")
    b.wait_for("document.getElementById('e-status').textContent.includes('Changed on another phone')")
    assert b.js("document.getElementById('e-qty').value") == "3 kg"   # refilled with the latest
    assert db_items(server)[0][1] == "3 kg"                              # first save kept
    b.js("document.getElementById('g-edit').close()")
    a.screenshot(str(artifacts / "grocery-phone-a.png"))
    dash.screenshot(str(artifacts / "grocery-dashboard.png"))

    # 422 (invalid input) and 409 (duplicate / edit conflict) were provoked on
    # purpose above; Chrome logs them as failed resources. Anything else fails.
    expected = ("status of 422", "status of 409")
    for p in (a, b, dash):
        unexpected = [e for e in p.errors if not any(x in e for x in expected)]
        assert unexpected == [], unexpected


def test_list_persists_after_backend_restart(chrome, server):
    a = phone(chrome, server)
    add_via_ui(a, "Paneer", "200 g", "dairy")
    a.wait_for("document.getElementById('g-groups').textContent.includes('Paneer')")
    row_click(a, "Paneer", "body")
    a.wait_for("document.getElementById('g-edit').open")
    a.js("document.getElementById('e-notes').value = 'Fresh, not frozen'; document.getElementById('g-edit-form').requestSubmit();")
    a.wait_for("document.getElementById('g-groups').textContent.includes('Fresh, not frozen')")

    server.restart()
    a.wait_for("document.getElementById('g-live').dataset.state === 'live'", timeout=15)  # reconnects by itself
    fresh = phone(chrome, server)
    fresh.wait_for("document.getElementById('g-groups').textContent.includes('Paneer')")
    assert "200 g" in fresh.text("#g-groups") and "Fresh, not frozen" in fresh.text("#g-groups")
    a.errors[:] = [e for e in a.errors if "ERR_CONNECTION_REFUSED" not in e and "/api/events" not in e]
    assert a.errors == [] and fresh.errors == []


def test_pin_mode_access_control(chrome, make_server):
    srv = make_server(AURA_EDIT_ACCESS="pin", AURA_HOUSEHOLD_PIN="482916")
    # The browser reaches the server via 127.0.0.1 (trusted local display),
    # so turn that trust off to behave like a phone on the Wi-Fi.
    srv.env["AURA_TRUST_LOCALHOST"] = "false"
    srv.restart()
    p = phone(chrome, srv)
    assert not p.js("document.getElementById('g-banner').hidden")
    assert p.js("document.getElementById('g-composer').classList.contains('is-locked')")
    p.click(".g-chip")                                          # tapping asks for the PIN
    p.wait_for("document.getElementById('g-signin').open")
    p.js("document.getElementById('s-pin').value = '000000'; document.getElementById('g-signin-form').requestSubmit();")
    p.wait_for("document.getElementById('s-status').textContent === 'Wrong PIN'")
    p.js("document.getElementById('s-pin').value = '482916'; document.getElementById('g-signin-form').requestSubmit();")
    p.wait_for("document.getElementById('g-banner').hidden")
    p.wait_for("document.querySelectorAll('.g-item').length === 1")   # the chip tap was retried after sign-in
    other = phone(chrome, srv)                                    # separate session: still locked
    assert not other.js("document.getElementById('g-banner').hidden")
    status, _ = srv.api("POST", "/api/groceries", {"name": "Eggs"})
    assert status == 401
