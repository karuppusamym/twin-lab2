"""Why a web step has no picture, in the completeness manifest the recorder sends.

Rebuild proof case A: the portal's Verify found 17 pictures that steps named but the recording
did not hold. The recorder had written them; they were removed from the folder afterwards (the
pictures with typed values of gap 16). The web channel's notes now say how many steps have a
picture, how many the recorder dropped (`picture_dropped`) and how many a step names that are not
in the folder, so a missing picture is never read as one the recorder failed to write.
Fixture folders only (no browser is started).
"""

import json
from pathlib import Path
import tempfile
import unittest

from twinlab2_recorder.workstation import completeness


def folder_with(steps, files):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "web" / "screens").mkdir(parents=True)
    (root / "web" / "steps.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    for name in files:
        (root / "web" / name).write_bytes(b"\x89PNG")
    return tmp, root


class PictureNote(unittest.TestCase):
    def test_kept_dropped_and_removed_pictures_are_counted(self):
        steps = [{"seq": 1, "screenshot": "screens/0001.png"},
                 {"seq": 2, "screenshot": None, "picture_dropped": True, "picture_note": "no picture: ..."},
                 {"seq": 3, "screenshot": "screens/0003.png"},
                 {"seq": 4, "screenshot": None}]  # fmt: skip
        tmp, root = folder_with(steps, ["screens/0001.png"])
        self.addCleanup(tmp.cleanup)
        note = completeness.picture_note(root)
        self.assertEqual(note, "pictures: 1 of 4 steps have one; 1 dropped by the recorder (the next action had "
                               "begun before it could be taken); 1 named by a step are not in the recording folder "
                               "(removed after recording, not compared)")  # fmt: skip
        self.assertLessEqual(len(note), completeness.NOTE_MAX)

    def test_the_note_is_the_web_channels_first(self):
        tmp, root = folder_with([{"seq": 1, "screenshot": "screens/0001.png"}], ["screens/0001.png"])
        self.addCleanup(tmp.cleanup)
        meta = {"targets": [{"origin": "https://orders.example.test", "level": "full"}],
                "channel_status": {"web": {"status": "complete", "gaps": [], "notes": ["other"] * 60}}}
        web = completeness.manifest(root, meta)["channels"]["web"]
        self.assertEqual(web["notes"][0], "pictures: 1 of 1 steps have one")
        self.assertEqual(len(web["notes"]), completeness.NOTES_MAX)

    def test_no_web_steps_no_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(completeness.picture_note(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
