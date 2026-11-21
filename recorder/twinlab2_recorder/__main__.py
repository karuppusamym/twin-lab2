"""python -m twinlab2_recorder: the same as TwinLabRecorder.exe."""

import multiprocessing
import sys

from twinlab2_recorder.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
