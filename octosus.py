#!/usr/bin/env python3

__version__ = "1.0.0"

import argparse
import hashlib
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing 'requests'. Install with: pip install requests")
    sys.exit(1)

try:
    from rich import box
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Missing 'rich'. Install with: pip install rich")
    sys.exit(1)


def parse_repo_url(url: str) -> tuple:
    patterns = [
        r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$",
        r"github\.com[:/]([^/]+)/([^/.]+)",
    ]
    for pat in patterns:
        m = re.search(pat, url.strip())
        if m:
            return m.group(1), m.group(2)
    raise ValueError(f"Could not parse GitHub URL: {url}")


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class KeyListener:
    def __init__(self):
        self.quit_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def _listen(self):
        try:
            import tty
            import termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not self.quit_event.is_set():
                    ch = sys.stdin.read(1)
                    if ch.lower() == "q":
                        self.quit_event.set()
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    @property
    def should_quit(self) -> bool:
        return self.quit_event.is_set()


class FetchError(Exception):
    pass


class Octosus:
    API = "https://api.github.com"
    RAW = "https://raw.githubusercontent.com"
    MAX_LOG = 14

    def __init__(self, owner: str, repo: str, output: str,
                 token: str | None = None, threads: int = 4):
        self.owner = owner
        self.repo = repo
        self.out = Path(output)
        self.threads = threads

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Octosus/1.0",
        })
        if token:
            self.session.headers["Authorization"] = f"token {token}"

        self.seen_blobs: set[str] = set()
        self.seen_trees: set[str] = set()
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self.total_commits = 0
        self.processed = 0
        self.files_encountered = 0
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.bytes_total = 0
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None
        self._log: list[tuple[str, str, str]] = []
        self.keys = KeyListener()
        self.aborted = False

    def log(self, msg: str, style: str = ""):
        ts = time.strftime("%H:%M:%S")
        with self._log_lock:
            self._log.append((ts, msg, style))
            if len(self._log) > self.MAX_LOG:
                self._log = self._log[-self.MAX_LOG:]

    def _check_rate(self, r: requests.Response):
        rem = r.headers.get("X-RateLimit-Remaining")
        rst = r.headers.get("X-RateLimit-Reset")
        if rem is not None:
            self.rate_remaining = int(rem)
        if rst is not None:
            self.rate_reset = int(rst)
        if self.rate_remaining is not None and self.rate_remaining < 10:
            wait = max(0, (self.rate_reset or int(time.time()) + 60) - int(time.time())) + 2
            self.log(f"Rate-limit low ({self.rate_remaining}). Sleeping {wait}s", "yellow")
            time.sleep(wait)

    def _api(self, path: str, params: dict | None = None) -> requests.Response:
        url = f"{self.API}{path}"
        while True:
            try:
                r = self.session.get(url, params=params, timeout=30)
            except requests.ConnectionError as exc:
                raise FetchError(f"Connection failed: {exc}")
            except requests.Timeout:
                raise FetchError("Request timed out after 30s")
            except requests.RequestException as exc:
                raise FetchError(f"Request error: {exc}")
            self._check_rate(r)
            if r.status_code == 403 and "rate limit" in r.text.lower():
                wait = max(0, (self.rate_reset or int(time.time()) + 60) - int(time.time())) + 2
                self.log(f"Rate-limited! Waiting {wait}s", "yellow")
                time.sleep(wait)
                continue
            return r

    def fetch_commits(self, on_progress=None) -> list[dict]:
        commits = []
        page = 1
        while True:
            r = self._api(f"/repos/{self.owner}/{self.repo}/commits", {
                "per_page": 100,
                "page": page,
            })
            if r.status_code == 404:
                raise FetchError("Repository not found (404). Check the URL and permissions.")
            if r.status_code == 401:
                raise FetchError("Authentication failed (401). Check your token.")
            if r.status_code == 403:
                raise FetchError(f"Access denied (403): {r.json().get('message', 'unknown')}")
            if r.status_code != 200:
                raise FetchError(f"API returned HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if not data:
                break
            for c in data:
                commits.append({
                    "sha": c["sha"],
                    "tree_sha": c["commit"]["tree"]["sha"],
                    "msg": c["commit"]["message"].split("\n")[0][:58],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                })
            if on_progress:
                on_progress(page, len(commits))
            if len(data) < 100:
                break
            page += 1
        commits.reverse()
        return commits

    def fetch_tree(self, tree_sha: str) -> list[dict]:
        r = self._api(f"/repos/{self.owner}/{self.repo}/git/trees/{tree_sha}", {
            "recursive": "1",
        })
        if r.status_code != 200:
            self.log(f"Tree {tree_sha[:8]} failed ({r.status_code})", "red")
            return []
        return [
            {"path": e["path"], "sha": e["sha"], "size": e.get("size", 0)}
            for e in r.json().get("tree", [])
            if e["type"] == "blob"
        ]

    def download(self, commit_sha: str, path: str) -> bytes | None:
        url = f"{self.RAW}/{self.owner}/{self.repo}/{commit_sha}/{path}"
        try:
            r = self.session.get(url, timeout=30)
            if r.status_code == 200:
                return r.content
            self.log(f"HTTP {r.status_code}: {path}", "yellow")
        except requests.RequestException as exc:
            self.log(f"Net error: {exc}", "red")
        return None

    def save(self, relpath: str, data: bytes):
        p = Path(relpath)
        sha = hashlib.sha256(data).hexdigest()[:12]
        dest = self.out / p.parent / f"{sha}{p.stem}{p.suffix}"
        with self._save_lock:
            dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        with self._lock:
            self.bytes_total += len(data)

    def _download_worker(self, commit_sha: str, fi: dict) -> bool:
        content = self.download(commit_sha, fi["path"])
        if content is not None:
            self.save(fi["path"], content)
            with self._lock:
                self.downloaded += 1
            self.log(f"  + {fi['path']}  ({fmt_bytes(len(content))})", "green")
            return True
        else:
            with self._lock:
                self.failed += 1
            self.log(f"  x {fi['path']}", "red")
            return False

    def _stats_table(self) -> Table:
        t = Table(
            box=box.SIMPLE_HEAVY,
            border_style="cyan",
            expand=True,
            show_header=False,
            padding=(0, 1),
        )
        t.add_column(style="bold bright_white", ratio=1)
        t.add_column(style="bold bright_green", justify="right", ratio=1)
        t.add_row("Commits total", str(self.total_commits))
        t.add_row("Processed", str(self.processed))
        t.add_row("Files seen", str(self.files_encountered))
        t.add_row("Unique blobs", str(len(self.seen_blobs)))
        t.add_row("Downloaded", str(self.downloaded))
        t.add_row("Skipped (dup)", str(self.skipped))
        t.add_row("Failed", str(self.failed))
        t.add_row("Data saved", fmt_bytes(self.bytes_total))
        t.add_row("Threads", str(self.threads))
        if self.rate_remaining is not None:
            t.add_row("API calls left", str(self.rate_remaining))
        return t

    def _log_panel(self) -> Panel:
        txt = Text()
        with self._log_lock:
            entries = list(self._log[-12:])
        for ts, msg, sty in entries:
            txt.append(f" {ts} ", style="dim")
            txt.append(f"{msg}\n", style=sty or "white")
        return Panel(
            txt,
            title="[bold bright_yellow]Activity Log[/]",
            border_style="yellow",
            padding=(0, 1),
        )

    def _layout(self, commit_prog: Progress, file_prog: Progress) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="bars", size=6),
            Layout(name="body"),
            Layout(name="footer", size=1),
        )
        bars = Table.grid(expand=True)
        bars.add_row(
            Panel(commit_prog, border_style="cyan", title="[bold]Commit Progress[/]", padding=(0, 1))
        )
        bars.add_row(
            Panel(file_prog, border_style="magenta", title="[bold]File Progress[/]", padding=(0, 1))
        )
        layout["bars"].update(bars)

        body = Table.grid(expand=True)
        body.add_row(
            Panel(
                self._stats_table(),
                title="[bold bright_green]Statistics[/]",
                border_style="green",
                padding=(0, 0),
            ),
            self._log_panel(),
        )
        layout["body"].update(body)
        layout["footer"].update(
            Text(" Press q to quit gracefully", style="dim italic")
        )
        return layout

    def _cleanup_output(self, existed_before: bool):
        if not existed_before and self.out.exists():
            shutil.rmtree(self.out, ignore_errors=True)

    def run(self):
        console = Console()
        out_existed = self.out.exists()

        console.print()
        console.print(Panel(
            "[bold bright_white]Octosus -- GitHub Snooper[/]\n"
            f"[dim]Snooping every unique file from[/] "
            f"[bold bright_cyan]{self.owner}/{self.repo}[/]\n"
            f"[dim]Output ->[/] [bright_green]{self.out.resolve()}[/]\n"
            f"[dim]Threads:[/] [bright_yellow]{self.threads}[/]",
            border_style="bright_magenta",
            padding=(1, 2),
        ))
        console.print()

        commits = None
        status_handle = None

        def _on_progress(page, count):
            if status_handle:
                status_handle.update(
                    f"[bold cyan]Fetching commit history  "
                    f"(page {page}, {count} commits)[/]"
                )

        while commits is None:
            try:
                with console.status(
                    "[bold cyan]Fetching commit history[/]"
                ) as status:
                    status_handle = status
                    commits = self.fetch_commits(on_progress=_on_progress)
                    status_handle = None

            except FetchError as exc:
                status_handle = None
                console.print()
                console.print(Panel(
                    f"[bold bright_red]Error fetching commits[/]\n\n"
                    f"[white]{exc}[/]\n\n"
                    f"[dim]Press [/dim][bold]r[/bold][dim] to retry or [/dim][bold]q[/bold][dim] to quit.[/dim]",
                    border_style="red",
                    title="[bold red]Fetch Failed[/]",
                    padding=(1, 2),
                ))

                import tty, termios
                try:
                    fd = sys.stdin.fileno()
                    old = termios.tcgetattr(fd)
                    tty.setcbreak(fd)
                    while True:
                        ch = sys.stdin.read(1).lower()
                        if ch == "r":
                            console.print("[bold cyan]Retrying...[/]\n")
                            break
                        if ch == "q":
                            self._cleanup_output(out_existed)
                            console.print("[dim]Exiting.[/]")
                            return
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                except Exception:
                    self._cleanup_output(out_existed)
                    return
                commits = None
                continue

        if not commits:
            console.print("[bold red]No commits found in this repository.[/]")
            self._cleanup_output(out_existed)
            return

        self.total_commits = len(commits)
        self.log(f"Found {self.total_commits} commits")
        self.out.mkdir(parents=True, exist_ok=True)

        self.keys.start()

        cprog = Progress(
            SpinnerColumn("dots"),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=40, style="bright_black", complete_style="cyan",
                      finished_style="bright_green"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        fprog = Progress(
            SpinnerColumn("dots2"),
            TextColumn("[bold magenta]{task.description}"),
            BarColumn(bar_width=40, style="bright_black", complete_style="magenta",
                      finished_style="bright_green"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        ctask = cprog.add_task("Commits", total=self.total_commits)
        ftask = fprog.add_task("Files", total=0)

        with Live(self._layout(cprog, fprog), console=console,
                  refresh_per_second=8, screen=False) as live:

            for commit in commits:
                if self.keys.should_quit:
                    self.aborted = True
                    self.log("Quit requested, stopping gracefully", "yellow")
                    live.update(self._layout(cprog, fprog))
                    break

                sha = commit["sha"]
                tree_sha = commit["tree_sha"]
                msg = commit["msg"]

                cprog.update(ctask, description=f"Commits  {sha[:8]}")
                self.log(f"[{sha[:8]}] {msg}", "bright_cyan")

                if tree_sha in self.seen_trees:
                    self.log("  Duplicate tree -- skipped", "dim")
                    self.processed += 1
                    cprog.advance(ctask)
                    live.update(self._layout(cprog, fprog))
                    continue
                self.seen_trees.add(tree_sha)

                files = self.fetch_tree(tree_sha)
                self.files_encountered += len(files)

                new = [f for f in files if f["sha"] not in self.seen_blobs]
                self.skipped += len(files) - len(new)

                if new:
                    for fi in new:
                        self.seen_blobs.add(fi["sha"])

                    fprog.reset(ftask)
                    fprog.update(ftask, total=len(new), completed=0,
                                 description=f"Files    {sha[:8]}")

                    with ThreadPoolExecutor(max_workers=self.threads) as pool:
                        futures = {
                            pool.submit(self._download_worker, sha, fi): fi
                            for fi in new
                        }
                        for future in as_completed(futures):
                            if self.keys.should_quit:
                                self.aborted = True
                                self.log("Quit requested, cancelling downloads", "yellow")
                                pool.shutdown(wait=False, cancel_futures=True)
                                break
                            future.result()
                            fprog.advance(ftask)
                            live.update(self._layout(cprog, fprog))

                    if self.aborted:
                        live.update(self._layout(cprog, fprog))
                        break
                else:
                    self.log("  No new unique files", "dim")

                self.processed += 1
                cprog.advance(ctask)
                live.update(self._layout(cprog, fprog))

        console.print()
        title = "[bold bright_white]Snooping Complete[/]"
        if self.aborted:
            title = "[bold bright_yellow]Snooping Aborted (partial)[/]"
        summ = Table(
            box=box.DOUBLE_EDGE,
            border_style="bright_green",
            title=title,
        )
        summ.add_column("", style="bold")
        summ.add_column("", style="bright_green", justify="right")
        summ.add_row("Commits processed", str(self.processed))
        summ.add_row("Files encountered", str(self.files_encountered))
        summ.add_row("Unique files downloaded", str(self.downloaded))
        summ.add_row("Skipped (duplicates)", str(self.skipped))
        summ.add_row("Failed downloads", str(self.failed))
        summ.add_row("Total data saved", fmt_bytes(self.bytes_total))
        summ.add_row("Output directory", str(self.out.resolve()))
        console.print(summ)
        console.print()


def main():
    parser = argparse.ArgumentParser(
        description="Octosus -- Download every unique file from a GitHub repo's full history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s https://github.com/user/repo
  %(prog)s https://github.com/user/repo -o ./dump
  %(prog)s https://github.com/user/repo --token ghp_xxxx
  %(prog)s https://github.com/user/repo -t 8

Environment:
  GITHUB_TOKEN   Used automatically if --token is not provided.
        """,
    )
    parser.add_argument(
        "-v", "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("repo_url", help="GitHub repository URL")
    parser.add_argument(
        "-o", "--output", default=".",
        help="Output directory (default: current directory)",
    )
    parser.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub personal access token (default: $GITHUB_TOKEN)",
    )
    parser.add_argument(
        "-t", "--threads", type=int, default=4,
        help="Number of download threads (default: 4)",
    )
    args = parser.parse_args()

    if args.threads < 1:
        print("Error: threads must be >= 1")
        sys.exit(1)

    try:
        owner, repo = parse_repo_url(args.repo_url)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    Octosus(owner, repo, args.output, args.token, args.threads).run()


if __name__ == "__main__":
    main()
