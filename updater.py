"""VeriFlow Studio - zero-dependency GitHub update checker and 1-click self-updater (standard library only).

Two sources are checked, the newest wins:
  1. GitHub Releases API   https://api.github.com/repos/<owner>/<repo>/releases/latest   (carries the .exe asset)
  2. version.json on main  https://raw.githubusercontent.com/<owner>/<repo>/main/version.json
so bumping version.json in a push to main is enough to notify every user running from source.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from config_manager import APP_VERSION

REPO_FALLBACK = "Nirco99/veriflow-studio"
USER_AGENT = "VeriFlow-Studio-Updater"
TIMEOUT = 4.0
CURL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED = 0x00000008 | 0x00000200 | NO_WINDOW if os.name == "nt" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP


def app_dir() -> Path:
    """Folder of the running program: the .exe folder when frozen, the source folder otherwise."""
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.4.0' / '1.4' / '1.10.2-beta' -> (1, 4, 0) / (1, 4, 0) / (1, 10, 2). Unparseable -> (0,)."""
    nums = [int(n) for n in re.findall(r"\d+", str(v).split("-")[0].split("+")[0])[:4]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums) if any(nums) else (0,)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def slug_from_url(url: str) -> str | None:
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def detect_repo_slug(folder: Path | None = None) -> str:
    """owner/repo from `git config --get remote.origin.url`, falling back to the official repository."""
    try:
        out = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=str(folder or app_dir()),
                             capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW).stdout
        return slug_from_url(out) or REPO_FALLBACK
    except (OSError, subprocess.SubprocessError):
        return REPO_FALLBACK


@dataclass
class UpdateInfo:
    version: str
    notes: str = ""
    page_url: str = ""              # release page / commits page ("View Changes")
    exe_url: str = ""               # .exe asset of a release with this version (frozen self-update)
    sources: list[str] = field(default_factory=list)


class AutoUpdater:
    def __init__(self, current: str = APP_VERSION, repo: str | None = None, opener: Callable | None = None,
                 timeout: float = TIMEOUT, folder: Path | None = None):
        self.current = current
        self.folder = Path(folder) if folder else app_dir()
        self.repo = repo or detect_repo_slug(self.folder)
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.last_error = ""

    # ---- network
    def _use_curl(self, exc: BaseException) -> bool:
        """Python's OpenSSL can't fetch missing intermediates / on-demand Windows roots ("unable to get issuer
        certificate"); Windows' own curl.exe uses the system certificate store (Schannel), so fall back to it."""
        reason = getattr(exc, "reason", exc)
        return self.opener is urllib.request.urlopen and CURL.is_file() and isinstance(reason, (ssl.SSLError, ssl.CertificateError))

    def _curl(self, url: str, dest: Path | None = None, timeout: float | None = None) -> bytes:
        cmd = [str(CURL), "-fsSL", "--max-time", str(int(timeout or self.timeout) + 1), "-H", f"User-Agent: {USER_AGENT}",
               "-H", "Accept: application/vnd.github+json", url]
        if dest is not None:
            cmd[1:1] = ["-o", str(dest)]
        p = subprocess.run(cmd, capture_output=True, timeout=(timeout or self.timeout) + 5, creationflags=NO_WINDOW)
        if p.returncode != 0:
            raise OSError(f"curl exit {p.returncode}: {p.stderr.decode(errors='replace').strip()[:200]}")
        return p.stdout

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
        try:
            with self.opener(req, timeout=self.timeout) as r:
                return r.read()
        except Exception as exc:
            if self._use_curl(exc):
                return self._curl(url)
            raise

    def _json(self, url: str) -> dict | None:
        try:
            data = json.loads(self._get(url).decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
        except Exception as exc:         # offline, rate-limited, 404 (no release yet), bad JSON: stay silent
            self.last_error = f"{url}: {exc}"
            return None

    def fetch_release(self) -> UpdateInfo | None:
        rel = self._json(f"https://api.github.com/repos/{self.repo}/releases/latest")
        if not rel or not rel.get("tag_name") or rel.get("draft") or rel.get("prerelease"):
            return None
        exe = next((a.get("browser_download_url", "") for a in rel.get("assets") or []
                    if str(a.get("name", "")).lower().endswith(".exe")), "")
        return UpdateInfo(version=str(rel["tag_name"]).lstrip("vV"), notes=str(rel.get("body") or rel.get("name") or ""),
                          page_url=str(rel.get("html_url") or f"https://github.com/{self.repo}/releases/latest"),
                          exe_url=exe, sources=["release"])

    def fetch_version_json(self) -> UpdateInfo | None:
        data = self._json(f"https://raw.githubusercontent.com/{self.repo}/main/version.json")
        if not data or not data.get("version"):
            return None
        return UpdateInfo(version=str(data["version"]).lstrip("vV"), notes=str(data.get("notes") or ""),
                          page_url=f"https://github.com/{self.repo}/commits/main", sources=["version.json"])

    def check(self) -> UpdateInfo | None:
        """The newest remote version if it is newer than the running one, else None. Never raises."""
        found = [i for i in (self.fetch_release(), self.fetch_version_json()) if i]
        if not found:
            return None
        best = max(found, key=lambda i: parse_version(i.version))
        if not is_newer(best.version, self.current):
            return None
        rel = next((i for i in found if "release" in i.sources), None)
        if rel and parse_version(rel.version) >= parse_version(best.version):
            best.exe_url = rel.exe_url              # a release exists for this version: its .exe can be installed
            best.page_url = rel.page_url
            best.notes = best.notes or rel.notes
        best.sources = [s for i in found for s in i.sources]
        return best

    def check_async(self, callback: Callable[[UpdateInfo | None], None]) -> threading.Thread:
        """Run check() on a daemon thread. callback runs on that thread: marshal to the UI via a queue."""
        t = threading.Thread(target=lambda: callback(self.check()), daemon=True, name="veriflow-update-check")
        t.start()
        return t

    # ---- applying an update
    def can_self_update(self, info: UpdateInfo) -> bool:
        return bool(info.exe_url) if is_frozen() else True

    def download(self, url: str, dest: Path, progress: Callable[[int], None] | None = None) -> Path:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            r = self.opener(req, timeout=30)
        except Exception as exc:
            if not self._use_curl(exc):
                raise
            self._curl(url, dest=tmp, timeout=600)
            os.replace(tmp, dest)
            return dest
        with r, open(tmp, "wb") as f:
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done)
        os.replace(tmp, dest)
        return dest

    def update_source(self, log: Callable[[str], None] = print) -> bool:
        """Source checkout: `git pull --rebase --autostash origin main`, or download main.zip when .git is absent."""
        if (self.folder / ".git").exists():
            p = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=str(self.folder),
                               capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
            log((p.stdout + p.stderr).strip())
            return p.returncode == 0
        with tempfile.TemporaryDirectory(prefix="veriflow_upd_") as td:
            z = self.download(f"https://github.com/{self.repo}/archive/refs/heads/main.zip", Path(td) / "main.zip")
            with zipfile.ZipFile(z) as zf:
                zf.extractall(Path(td) / "x")
            roots = [d for d in (Path(td) / "x").iterdir() if d.is_dir()]
            if not roots or not (roots[0] / "main.py").is_file():
                log("Downloaded archive does not look like VeriFlow Studio - update aborted.")
                return False
            for src in roots[0].rglob("*"):
                if src.is_file() and ".git" not in src.parts:
                    dst = self.folder / src.relative_to(roots[0])
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            log(f"Updated the files in {self.folder} from main.zip")
        return True

    @staticmethod
    def swap_script(new_exe: Path, target_exe: Path, relaunch: bool = True, tries: int = 30) -> str:
        """cmd helper: wait until the running .exe is released, move the new one over it, relaunch, delete itself.
        ping is used as the delay because `timeout` refuses to run without a console."""
        return "\r\n".join([
            "@echo off",
            "set n=0",
            ":retry",
            "ping -n 2 127.0.0.1 >nul",
            f'move /y "{new_exe}" "{target_exe}" >nul 2>&1',
            "if not errorlevel 1 goto done",
            "set /a n+=1",
            f"if %n% lss {tries} goto retry",
            "goto end",
            ":done",
            f'start "" "{target_exe}"' if relaunch else "rem no relaunch",
            ":end",
            'del "%~f0"',
            ""])

    def update_frozen(self, info: UpdateInfo, progress: Callable[[int], None] | None = None) -> Path:
        """Download the release .exe next to the running one and spawn the detached swap helper. Returns the helper."""
        target = Path(sys.executable).resolve()
        new = target.with_name(target.stem + "_new.exe")
        self.download(info.exe_url, new, progress)
        with open(new, "rb") as f:
            if f.read(2) != b"MZ" or new.stat().st_size < 1_000_000:
                new.unlink(missing_ok=True)
                raise OSError("The downloaded file is not a valid VeriFlow Studio executable.")
        helper = Path(tempfile.gettempdir()) / f"veriflow_update_{os.getpid()}.cmd"
        helper.write_text(self.swap_script(new, target), encoding="mbcs" if os.name == "nt" else "utf-8")
        subprocess.Popen(["cmd.exe", "/c", str(helper)], creationflags=DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return helper

    @staticmethod
    def restart_command() -> list[str]:
        """How to relaunch the program (a fresh process; os.execl mangles paths with spaces on Windows)."""
        if is_frozen():
            return [sys.executable, *sys.argv[1:]]
        return [sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]]
