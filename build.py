#!/usr/bin/env python3

import subprocess
import sys

def main():
    try:
        import PyInstaller
    except ImportError:
        print("PyInstaller not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "octosus",
        "--clean",
        "--noconfirm",
        "octosus.py",
    ]

    print(f"Running: {' '.join(cmd)}")
    subprocess.check_call(cmd)
    print("\nBuild complete: ./dist/octosus")

if __name__ == "__main__":
    main()
