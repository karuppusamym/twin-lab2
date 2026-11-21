"""The parts of Twin Lab that TwinLab2 copied, and what it left out.

TwinLab2 carries its own copy of the Twin Lab code it runs (docs/UPSTREAM.md lists each module
and the commit, 90bdd91). Paths of that code that only Twin Lab's own command line or its pack
builder reach were cut from the copy. A cut path raises `NotAbsorbed` instead of running, so a
caller that reaches one anyway reports it plainly: Verify turns it into a scenario that was not
run (never a pass), and the recorder into a note.
"""

UPSTREAM = "https://github.com/karuppusamym/twin-lab"
UPSTREAM_COMMIT = "90bdd917414a24a4da7f09fb66e8edb49ba3bfee"


class NotAbsorbed(RuntimeError):
    """A Twin Lab code path that is not part of TwinLab2's copy."""

    def __init__(self, what: str):
        super().__init__(f"{what} is not part of TwinLab2 (cut from the copy of Twin Lab "
                         f"{UPSTREAM_COMMIT[:7]}; docs/UPSTREAM.md)")
        self.what = what
