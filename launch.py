"""Launcher: pre-imports heavy C-extension libs on the main thread, then starts Streamlit.

Works around a Python 3.14 concurrent-import race ("acquire_lock returned a result with
an exception set" / partially initialized module 'pandas') when Streamlit's threads import
pandas at the same time.
"""
import sys
from pathlib import Path

import numpy  # noqa: F401
import pandas  # noqa: F401
import pyarrow  # noqa: F401
import openpyxl  # noqa: F401
import requests  # noqa: F401

from streamlit.web import cli

if __name__ == "__main__":
    app = Path(__file__).resolve().with_name("app.py")
    sys.argv = ["streamlit", "run", str(app), *sys.argv[1:]]
    sys.exit(cli.main())
