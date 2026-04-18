# Octosus

A command-line tool that crawls the entire commit history of a GitHub repository and downloads every unique file that ever existed in it.

## What it does

Octosus iterates through every commit in a repository (oldest to newest), inspects the file tree at each commit, and downloads any file whose content (blob SHA) hasn't been seen before. This means you get every unique version of every file that ever existed in the repo — including files that were later deleted or overwritten.

- Files are downloaded via `raw.githubusercontent.com`, which does **not** count against the GitHub API rate limit.
- Duplicate trees across commits are automatically skipped.
- Files are saved at their original repository paths inside the output directory.

## Installation

```bash
git clone https://github.com/youruser/octosus.git
cd octosus
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```
python octosus.py <repo_url> [options]
```

### Arguments

| Argument | Description |
|---|---|
| `repo_url` | GitHub repository URL (HTTPS or SSH format) |
| `-o`, `--output` | Output directory (default: current directory) |
| `--token` | GitHub personal access token |
| `-t`, `--threads` | Number of download threads (default: 4) |

### Examples

```bash
# Basic usage
python octosus.py https://github.com/octocat/Hello-World

# Custom output directory
python octosus.py https://github.com/user/repo -o ./dump

# With auth token for higher rate limits and 8 threads
python octosus.py https://github.com/user/repo --token ghp_xxxx -t 8

# Using environment variable for token
export GITHUB_TOKEN=ghp_xxxx
python octosus.py https://github.com/user/repo
```

## TUI Controls

| Key | Action |
|---|---|
| `q` | Quit gracefully (finishes in-flight downloads, then stops) |

## Rate Limits

| Auth | Limit |
|---|---|
| No token | 60 API requests/hour |
| With token | 5,000 API requests/hour |

Each commit uses 1-2 API calls (commit listing + tree fetch). File downloads via `raw.githubusercontent.com` are unlimited. For repositories with many commits, providing a token with `--token` or `GITHUB_TOKEN` is strongly recommended.

## Building

Build a standalone binary with no Python dependency required at runtime:

```bash
# Using the build script
python build.py

# Or directly with PyInstaller
pip install pyinstaller
pyinstaller --onefile --name octosus --clean octosus.py
```

The binary will be at `./dist/octosus`. Copy it anywhere on your `$PATH`:

```bash
sudo cp dist/octosus /usr/local/bin/
octosus https://github.com/user/repo
```

## Requirements

- Python 3.10+
- `requests`
- `rich`
- `pyinstaller` (build only)

# ⚠️ AI Disclosure

> [!WARNING]
> This script was made with the assistance of AI tools, due to my laziness and swift requirement to get this done. Please review the code carefully before using it in production environments.