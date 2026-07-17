"""Test environment setup.

Color constants bake at import time from TTY detection (models._colors_enabled).
Force them on so color-behavior assertions hold under pytest's captured
(non-tty) stdout. Subprocess-based tests control their own environment.
"""

import os

os.environ["FORCE_COLOR"] = "1"
