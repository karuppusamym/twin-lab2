"""The desktop picture comparison leaves the twin's label out (title-bar suffix, `TwinAppLabel`
footer), as the structure comparison and the web and 3270 replays do. No window is opened."""

import io
import os
import unittest
from unittest import mock

from PIL import Image
from twinlab2.replay import desktop

RAW = {"type": "Window", "name": "Cases", "children": [
    {"type": "TitleBar", "rect": [0, 0, 100, 10], "children": [
        {"type": "Button", "rect": [90, 0, 10, 10]}]},
    {"type": "Pane", "rect": [0, 10, 100, 80], "children": [
        {"type": "Text", "aid": "TwinAppLabel", "name": "AI generated Twin App", "rect": [0, 90, 100, 10]}]}]}


def png(label_colour):
    im = Image.new("RGB", (100, 100), (240, 240, 240))
    for x in range(0, 90):  # title text
        for y in range(0, 10):
            im.putpixel((x, y), label_colour)
    for x in range(100):  # footer label
        for y in range(90, 100):
            im.putpixel((x, y), label_colour)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


class LabelMask(unittest.TestCase):
    def test_boxes_are_the_title_text_and_the_footer_label(self):
        self.assertEqual(desktop.label_boxes(RAW), [[0, 0, 90, 10], [0, 90, 100, 10]])

    def test_a_picture_that_differs_only_at_the_label_matches_once_masked(self):
        reference, twin = png((10, 10, 10)), png((200, 0, 0))
        self.assertGreater(desktop.picture_difference(reference, twin), 10)
        boxes = desktop.label_boxes(RAW)
        self.assertEqual(desktop.picture_difference(desktop.mask_boxes(reference, boxes),
                                                    desktop.mask_boxes(twin, boxes)), 0.0)

    def test_a_difference_outside_the_label_still_fails(self):
        reference, twin = png((10, 10, 10)), Image.open(io.BytesIO(png((10, 10, 10)))).convert("RGB")
        twin.putpixel((50, 50), (0, 0, 0))
        twin.putpixel((51, 50), (0, 0, 0))
        out = io.BytesIO()
        twin.save(out, "PNG")
        boxes = desktop.label_boxes(RAW)
        self.assertGreater(desktop.picture_difference(desktop.mask_boxes(reference, boxes),
                                                      desktop.mask_boxes(out.getvalue(), boxes)), 0)

    def test_compare_picture_masks_only_when_the_label_is_required(self):
        class Live:
            label_at = desktop.label_boxes(RAW)

            @staticmethod
            def picture(_h):
                return png((200, 0, 0))

        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as d:
            rec = Path(d) / "rec"
            rec.mkdir()
            (rec / "p.png").write_bytes(png((10, 10, 10)))
            r = SimpleNamespace(path=rec)
            for env, fails in (("1", False), ("0", True)):
                with mock.patch.dict(os.environ, {"TWINLAB2_TWIN_LABEL": env}):
                    _note, diffs = desktop._compare_picture(Live, 1, r, {"seq": 1, "picture": "p.png"},
                                                            Path(d) / "out", 2.0)
                self.assertEqual(bool(diffs), fails)


if __name__ == "__main__":
    unittest.main()
