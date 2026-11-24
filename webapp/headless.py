"""Headless entry point for the unchanged OMRChecker CLI."""
import logging
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import screeninfo


def graphical_call(*args, **kwargs):
    raise RuntimeError("Graphical OMRChecker features are disabled")


def main():
    # Upstream asks for monitor dimensions at import, even with previews disabled.
    screeninfo.get_monitors = lambda: [SimpleNamespace(width=1280, height=900)]
    for name in ("imshow", "namedWindow", "destroyAllWindows", "destroyWindow",
                 "waitKey", "moveWindow", "getWindowProperty"):
        setattr(cv2, name, graphical_call)
    cv2.setNumThreads(1)
    handler = logging.StreamHandler()
    handler.addFilter(lambda record: not record.getMessage().startswith("Read Response:"))
    logging.basicConfig(handlers=[handler], level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")
    sys.tracebacklimit = 0
    engine = Path(__file__).resolve().parents[1] / "main.py"
    runpy.run_path(str(engine), run_name="__main__")


if __name__ == "__main__":
    main()
