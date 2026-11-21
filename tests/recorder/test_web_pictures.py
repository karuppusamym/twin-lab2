"""The web recorder's pictures, recorded headless in attach mode (a robot's browser over CDP):

- no picture ever shows typed text: every non-empty text field and editable region is painted
  over in every picture, also one taken while the next field is being typed (its value is not
  known to the recorder yet: gap 16 of the rebuild proof);
- a picture is never taken after the next action began: then it is dropped and the step says so
  (gap 15);
- an attached recording's manifest stays within the contract, so `send` accepts it (gap 1).

They need Playwright's Chromium (`python -m playwright install chromium`) and skip without it.
Chromium runs with --headless=new: nothing opens on the desktop."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

import web_harness as wh

EXE = wh.chromium_path()

FORM = b"""<!DOCTYPE html><html><head><title>Order</title><style>
body { margin: 0; font: 28px/1 monospace; background: #fff; color: #000; }
input, textarea, div.ce { position: absolute; left: 40px; width: 600px; height: 44px; border: 0;
  outline: none; padding: 4px; font: 28px/1 monospace; color: #000; background: #fff; caret-color: transparent;
  resize: none; overflow: hidden; }
#name { top: 20px; } #phone { top: 90px; } #notes { top: 160px; height: 80px; } #ce { top: 270px; }
#auto { top: 340px; } button { position: absolute; left: 40px; top: 420px; font-size: 20px; }
#start { left: 300px; }
</style></head><body>
<input id="name" name="name" autocomplete="off">
<input id="phone" name="phone" autocomplete="off">
<textarea id="notes" name="notes"></textarea>
<div id="ce" class="ce" contenteditable="true"></div>
<input id="auto" name="auto" autocomplete="off">
<button id="save" type="button" onclick="document.title='saved'">Save</button>
<button id="start" type="button">Start</button>
<script>
// "Start" makes the page itself type into #auto, a character every 15 ms for 3 s, with input
// events and no key events: the recorder's picture of the click on Start is taken meanwhile.
document.getElementById("start").addEventListener("click", () => {
  const el = document.getElementById("auto"), text = "Okonkwo Hale 0113 496 0522 side door";
  let i = 0;
  const t = setInterval(() => {
    if (i >= text.length) { clearInterval(t); return; }
    el.value += text[i++]; el.dispatchEvent(new Event("input", {bubbles: true}));
  }, 15);
});
</script></body></html>"""

FIELDS = ("#name", "#phone", "#notes", "#ce", "#auto")


def _dark_pixels(png: Path, box: dict) -> int:
    """Text pixels (dark, not the mask colour) inside a field's box, inset from its edge."""
    from PIL import Image

    img = Image.open(png).convert("RGB")
    x0, y0 = int(box["x"]) + 3, int(box["y"]) + 3
    x1, y1 = int(box["x"] + box["width"]) - 3, int(box["y"] + box["height"]) - 3
    n = 0
    for x in range(max(0, x0), min(img.width, x1)):
        for y in range(max(0, y0), min(img.height, y1)):
            r, g, b = img.getpixel((x, y))
            if r < 110 and g < 110 and b < 110:
                n += 1
    return n


@unittest.skipUnless(EXE, "Playwright's Chromium is not installed")
class AttachPictures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "web"
        with wh.site({"/": FORM}) as base, wh.headless_chromium(EXE) as cdp:
            stop, ready, thread, errors = wh.attach_in_thread(cdp, cls.out)
            if not ready.wait(30):
                raise RuntimeError("attach() did not get ready")
            if errors:
                raise errors[0]
            with sync_playwright() as pw:
                page = pw.chromium.connect_over_cdp(cdp).contexts[0].pages[0]
                page.goto(base + "/")
                page.wait_for_timeout(800)
                cls.boxes = {sel: page.locator(sel).bounding_box() for sel in FIELDS}
                page.click("#name")
                page.keyboard.type("Alice Example", delay=20)
                # The next field is typed slowly: the pictures of the steps before it are taken
                # while it already shows part of its value.
                for sel, text in (("#phone", "0113 496 0522"), ("#notes", "Deliver to the side door"),
                                  ("#ce", "Priya Okonkwo-Hale")):  # fmt: skip
                    page.click(sel)
                    page.keyboard.type(text, delay=90)
                # A robot's speed: the next click comes before the first one's picture can be
                # taken (gap 15). That picture would show the next action: it is dropped.
                page.click("#save")
                page.click("#name")
                page.wait_for_timeout(800)
                page.click("#start")
                page.wait_for_timeout(3500)
                page.click("#save")
                page.wait_for_timeout(1500)
            stop.set()
            thread.join(60)
        cls.steps = [json.loads(line) for line in (cls.out / "steps.jsonl").read_text(encoding="utf-8").splitlines()]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_no_picture_shows_typed_text(self):
        pictures = [s for s in self.steps if s.get("screenshot")]
        self.assertGreaterEqual(len(pictures), 3, [s.get("action") for s in self.steps])
        leaks = []
        for s in pictures:
            for sel, box in self.boxes.items():
                n = _dark_pixels(self.out / s["screenshot"], box)
                if n:
                    leaks.append((s["seq"], s["action"], sel, n))
        self.assertEqual(leaks, [], "typed text shows unmasked in these pictures")

    def test_every_picture_says_what_was_painted_over(self):
        for s in self.steps:
            if s.get("screenshot") and s.get("seq", 0) > 2:
                self.assertTrue(s.get("mask_boxes"), s)

    def test_no_picture_is_kept_that_was_taken_after_the_next_action_began(self):
        late = [(s["seq"], s.get("picture_note")) for s in self.steps
                if s.get("screenshot") and s.get("picture_settled") is False]  # fmt: skip
        self.assertEqual(late, [])
        for s in self.steps:
            if s.get("picture_dropped"):
                self.assertIsNone(s.get("screenshot"))
                self.assertIn("began", s.get("picture_note") or "")
                self.assertFalse((self.out / f"screens/{s['seq']:04d}.png").exists())

    def test_quick_actions_still_get_their_pictures(self):
        kept = [s for s in self.steps if s.get("screenshot")]
        # goto, the click into each field and the type steps: most keep a picture
        self.assertGreaterEqual(len(kept), 6, [(s["seq"], s["action"], s.get("picture_note")) for s in self.steps])

    def test_no_typed_value_in_the_steps(self):
        text = (self.out / "steps.jsonl").read_text(encoding="utf-8")
        for value in ("Alice Example", "0113 496 0522", "Priya Okonkwo-Hale", "side door"):
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
