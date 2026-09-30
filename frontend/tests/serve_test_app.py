"""Isolated browser-test factory, never a production config override."""
import os
from pathlib import Path
from app.api import create_app

def make_app():
    return create_app(Path(os.environ["FORECASTLAB_BROWSER_TEST_DATA"]))
