"""Indgang til PyInstaller: ``pyinstaller --onefile main.py``."""

from example_counter_python import Example
from thehome_plugin import run

if __name__ == "__main__":
    run(Example())
