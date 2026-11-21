"""Desktop pictures (twinlab2_recorder.desktop.pictures.places): a text box is painted over even when
the tree showed it empty, because the window's pixels are read after its tree and what is typed in
between must not show (the same race as gap 16 of the rebuild proof, on the desktop). Plain
Python on fixture trees: no window is opened."""

import unittest

from twinlab2_recorder.desktop import pictures


def tree(value):
    edit = {"type": "Edit", "name": "Customer", "rect": [10, 40, 200, 24], "visible": True, "hwnd": 7}
    if value is not None:
        edit["value"] = value
    label = {"type": "Text", "name": "Customer", "rect": [10, 10, 200, 20], "visible": True}
    return {"type": "Window", "name": "Orders", "hwnd": 1, "rect": [0, 0, 400, 300],
            "children": [label, edit]}  # fmt: skip


class EmptyTextBoxes(unittest.TestCase):
    def test_an_empty_text_box_is_still_painted_over(self):
        for value in (None, "", "  "):
            raw = tree(value)
            found = pictures.places(raw, raw)
            self.assertEqual([p["rect"] for p in found], [[10, 40, 200, 24]], value)
            self.assertIsNone(found[0]["secret"])  # nothing to look for with OCR
            self.assertEqual(pictures.secrets_of(found), set())

    def test_a_filled_text_box_keeps_its_value_for_the_ocr_check(self):
        raw = tree("Priya Okonkwo-Hale")
        clean = tree("Aaaaa Aaaaaaa-Aaaa")
        found = pictures.places(raw, clean)
        self.assertEqual(found[0]["secret"], "Priya Okonkwo-Hale")
        self.assertEqual(found[0]["text"], "Aaaaa Aaaaaaa-Aaaa")

    def test_a_label_is_not_painted_over(self):
        raw = tree(None)
        self.assertNotIn([10, 10, 200, 20], [p["rect"] for p in pictures.places(raw, raw)])


if __name__ == "__main__":
    unittest.main()
