"""VeriFlow Studio - global Verilog package manager ("import once, resolve anywhere on the PC").

Packages are folders of .v files:
  * builtin - the course library (`lib_dir`, normally C:\\iverilog\\compstruct\\lib); can be disabled, never removed
  * vault   - copies kept by VeriFlow in the global package vault (~/.veriflow_studio/packages/<name>)
  * linked  - an existing folder registered in place (scanned recursively)

At compile time every enabled package folder is passed with `-y`, and any needed module that `-y` cannot find
(file name != module name, several modules in one file, files in sub-folders) is loaded with `-l <file>`.
If such a file also defines a module that the student's own code (or a higher-priority package) defines,
a cached copy with that module blanked out is passed instead, so iverilog never reports "already declared".
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import zipfile
from pathlib import Path

from config_manager import BUILTIN_PACKAGE_NAME
from verilog_parser import (PACKAGE_SOURCE_EXTS, build_global_module_index, parse_all_modules_in_file,
                            parse_verilog_source, resolve_required_library_files, strip_modules_from_source)

HEADER_EXTS = (".vh", ".svh")          # `include headers: copied with packages, exposed through -I, never indexed as modules

DEFAULT_IMPORT_PACKAGE = "Imported_Course_Modules"
MAX_ZIP_BYTES = 64 * 1024 * 1024          # refuse absurd archives (zip bombs)
MAX_FILES = 3000


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name.strip()).strip(" .")
    return name[:80] or DEFAULT_IMPORT_PACKAGE


class PackageManager:
    def __init__(self, config):
        self.cfg = config
        self._cache: dict = {}

    # ------------------------------------------------------------------ registry
    @property
    def vault(self) -> Path:
        return Path(os.path.expanduser(self.cfg["global_packages_dir"]))

    def packages(self) -> list[dict]:
        """Registered packages with resolved paths and module counts (built-in path follows lib_dir)."""
        out = []
        for p in self.cfg["library_packages"]:
            path = Path(self.cfg["lib_dir"]) if p["kind"] == "builtin" else Path(p["path"])
            out.append({**p, "path": str(path), "exists": path.is_dir()})
        return out

    def _save(self, pkgs: list[dict]) -> None:
        self.cfg.update(library_packages=copy.deepcopy(pkgs))

    def _find(self, name_or_path: str) -> int:
        key = os.path.normcase(str(name_or_path))
        for i, p in enumerate(self.cfg["library_packages"]):
            if p["name"] == name_or_path or os.path.normcase(p["path"]) == key:
                return i
        return -1

    def _register(self, name: str, path: Path, kind: str) -> dict:
        pkgs = copy.deepcopy(self.cfg["library_packages"])
        i = self._find(str(path))
        if i >= 0:
            pkgs[i]["enabled"] = True
            entry = pkgs[i]
        else:
            base, k = name, 2
            while any(p["name"] == name for p in pkgs):
                name, k = f"{base} ({k})", k + 1
            entry = {"name": name, "path": str(path), "enabled": True, "builtin": False, "kind": kind}
            pkgs.append(entry)
        self._save(pkgs)
        return entry

    def toggle_package_enabled(self, package_name_or_path: str, enabled: bool) -> bool:
        pkgs = copy.deepcopy(self.cfg["library_packages"])
        i = self._find(package_name_or_path)
        if i < 0:
            return False
        pkgs[i]["enabled"] = bool(enabled)
        self._save(pkgs)
        return True

    def remove_package(self, package_name_or_path: str, delete_vault_files: bool = True) -> dict:
        """Unregister a package. Vault copies are deleted; linked folders are only forgotten, never deleted."""
        pkgs = copy.deepcopy(self.cfg["library_packages"])
        i = self._find(package_name_or_path)
        if i < 0:
            return {"ok": False, "message": "package not found"}
        p = pkgs[i]
        if p["kind"] == "builtin":
            return {"ok": False, "message": "the built-in course library can be disabled but not removed"}
        deleted = False
        if p["kind"] == "vault" and delete_vault_files:
            path = Path(p["path"])
            if path.is_dir() and self._inside_vault(path):
                shutil.rmtree(path, ignore_errors=True)
                deleted = True
        del pkgs[i]
        self._save(pkgs)
        return {"ok": True, "message": f"removed '{p['name']}'" + (" and deleted its vault folder" if deleted else ""),
                "deleted_files": deleted}

    def _inside_vault(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.vault.resolve())
            return path.resolve() != self.vault.resolve()
        except ValueError:
            return False

    # ------------------------------------------------------------------ imports
    def _unique_target(self, dest: Path, name: str, hint: str = "") -> Path:
        target = dest / name
        if target.exists() and hint:
            target = dest / f"{safe_name(hint)}_{name}"
        return target

    def _summarize(self, entry: dict | None, files: list[Path], skipped: list[str], blocked: list[dict]) -> dict:
        modules = sorted({m["module_name"] for f in files if f.suffix.lower() in PACKAGE_SOURCE_EXTS
                          for m in parse_all_modules_in_file(f)})
        return {"package": entry["name"] if entry else None, "path": entry["path"] if entry else None,
                "files": [f.name for f in files], "modules": modules, "skipped": skipped, "blocked": blocked}

    # ------------------------------------------------------------------ global module registry
    def module_map(self, include_disabled: bool = True) -> dict[str, str]:
        """Map<ModuleName, AbsoluteFilePath> over every registered package (first definition wins).

        Disabled packages count too: re-enabling one must never introduce a duplicate module declaration."""
        idx = build_global_module_index(self._lib_specs(enabled_only=not include_disabled), None, self._cache)
        return {n: str(Path(e["filepath"]).resolve()) for n, e in idx.items()}

    @staticmethod
    def _conflicts(text: str, target: Path, registry: dict[str, str]) -> list[dict]:
        """Modules in `text` that the registry already maps to a different file (re-importing the same file is fine)."""
        tkey = os.path.normcase(str(target.resolve()))
        return [{"module": m["module_name"], "existing": registry[m["module_name"]]}
                for m in parse_verilog_source(text)
                if m["module_name"] in registry and os.path.normcase(registry[m["module_name"]]) != tkey]

    @staticmethod
    def _claim(text: str, target: Path, registry: dict[str, str]) -> None:
        for m in parse_verilog_source(text):
            registry.setdefault(m["module_name"], str(target.resolve()))

    def _vet(self, label: str, text: str, target: Path, registry: dict[str, str], blocked: list[dict]) -> bool:
        """True = accept (and reserve its module names for the rest of this batch); False = blocked."""
        hits = self._conflicts(text, target, registry)
        if hits:
            blocked.append({"file": label, "conflicts": hits})
            return False
        self._claim(text, target, registry)
        return True

    def _finish(self, name: str, dest: Path, kind: str, copied: list[Path], skipped: list[str], blocked: list[dict]) -> dict:
        known = self._find(str(dest)) >= 0
        if not copied and not known:                     # everything blocked -> do not create an empty package
            try:
                dest.rmdir()
            except OSError:
                pass
            return self._summarize(None, [], skipped, blocked)
        return self._summarize(self._register(name, dest, kind), copied, skipped, blocked)

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------ imports (duplicate modules are blocked)
    def import_files_to_package(self, file_paths: list, package_name: str = DEFAULT_IMPORT_PACKAGE) -> dict:
        """Copy .v/.sv modules (and .vh headers) into the vault package `package_name`.

        A file defining a module that already exists in any registered package is *blocked* - iverilog would fail with
        "Module X was already declared" - and reported in result["blocked"]."""
        registry = self.module_map()
        dest = self.vault / safe_name(package_name)
        dest.mkdir(parents=True, exist_ok=True)
        copied, skipped, blocked = [], [], []
        for src in map(Path, file_paths):
            ext = src.suffix.lower()
            if ext not in PACKAGE_SOURCE_EXTS + HEADER_EXTS or not src.is_file():
                skipped.append(src.name)
                continue
            target = dest / src.name
            if ext in PACKAGE_SOURCE_EXTS and not self._vet(src.name, self._read(src), target, registry, blocked):
                continue
            if os.path.normcase(str(src.resolve())) != os.path.normcase(str(target.resolve())):
                shutil.copy2(src, target)
            copied.append(target)
        return self._finish(safe_name(package_name), dest, "vault", copied, skipped, blocked)

    def import_zip_package(self, zip_path, package_name: str | None = None) -> dict:
        """Extract every .v/.sv/.vh from a course .zip (flattened, path-traversal safe) into a vault package."""
        zip_path = Path(zip_path)
        name = safe_name(package_name or zip_path.stem)
        dest = self.vault / name
        dest.mkdir(parents=True, exist_ok=True)
        registry = self.module_map()
        copied, skipped, blocked, total = [], [], [], 0
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                parts = [p for p in re.split(r"[\\/]+", info.filename) if p]
                if info.is_dir() or not parts or "__MACOSX" in parts or parts[-1].startswith("."):
                    continue
                ext = Path(parts[-1]).suffix.lower()
                if ext not in PACKAGE_SOURCE_EXTS + HEADER_EXTS:
                    skipped.append(info.filename)
                    continue
                total += info.file_size
                if total > MAX_ZIP_BYTES or len(copied) >= MAX_FILES:
                    raise ValueError("archive too large for a Verilog package")
                data = zf.read(info)
                target = self._unique_target(dest, safe_name(parts[-1]), parts[-2] if len(parts) > 1 else "")
                if ext in PACKAGE_SOURCE_EXTS and not self._vet(info.filename, data.decode("utf-8", "replace"),
                                                                target, registry, blocked):
                    continue
                target.write_bytes(data)
                copied.append(target)
        return self._finish(name, dest, "vault", copied, skipped, blocked)

    def add_external_library_folder(self, folder_path, name: str | None = None, copy_to_vault: bool = False) -> dict:
        """Register a folder in place (linked, scanned recursively) or copy its .v/.vh files into the vault.

        A linked folder is all-or-nothing: if any of its modules is already registered elsewhere (or defined twice
        inside the folder) the link is refused, because iverilog would see duplicate module declarations."""
        folder = Path(folder_path)
        if not folder.is_dir():
            raise ValueError(f"not a folder: {folder}")
        display = (name or folder.name).strip() or folder.name
        name = safe_name(display)                 # folder-safe name only matters for vault copies
        registry = self.module_map()
        files = sorted((p for p in folder.rglob("*") if p.suffix.lower() in PACKAGE_SOURCE_EXTS + HEADER_EXTS and p.is_file()),
                       key=lambda p: str(p).lower())[:MAX_FILES]
        blocked: list[dict] = []
        if not copy_to_vault:
            if self._find(str(folder)) < 0:
                for f in files:
                    if f.suffix.lower() in PACKAGE_SOURCE_EXTS:
                        self._vet(str(f.relative_to(folder)), self._read(f), f, registry, blocked)
            if blocked:
                return self._summarize(None, [], [], blocked)
            return self._summarize(self._register(display, folder, "linked"), files, [], [])
        dest = self.vault / name
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for src in files:
            target = self._unique_target(dest, src.name, src.parent.name if src.parent != folder else "")
            if src.suffix.lower() in PACKAGE_SOURCE_EXTS and not self._vet(str(src.relative_to(folder)), self._read(src),
                                                                           target, registry, blocked):
                continue
            shutil.copy2(src, target)
            copied.append(target)
        return self._finish(name, dest, "vault", copied, [], blocked)

    def include_dirs(self) -> list[str]:
        """Folders of enabled packages that hold `include headers (.vh/.svh) -> passed to iverilog with -I."""
        out: list[str] = []
        for p in self.packages():
            if not (p["enabled"] and p["exists"]):
                continue
            root = Path(p["path"])
            for f in (root.rglob("*") if p["kind"] == "linked" else root.iterdir()):
                if f.suffix.lower() in HEADER_EXTS and f.is_file() and str(f.parent) not in out:
                    out.append(str(f.parent))
        return out

    def editor_collisions(self, path, text: str, work_dir, extra_files: list | tuple = ()) -> list[dict]:
        """Live duplicate-module check for an (unsaved) editor buffer - catches a module renamed in the editor
        to a name another workspace file or a package already uses."""
        path = Path(path)
        diags: list[dict] = []
        if not work_dir:
            return diags
        ws = build_global_module_index([], work_dir, self._cache, exclude_files=[path])
        pk = self.build_index(None)
        extras = {os.path.normcase(str(Path(work_dir) / f)) for f in extra_files}
        for m in parse_verilog_source(text):
            n = m["module_name"]
            if n in ws:
                other = Path(ws[n]["filepath"])
                compiled = os.path.normcase(str(other)) in extras
                diags.append({"file": str(path), "line": m["line"], "col": 0, "source": "registry", "code": "duplicate-module",
                              "severity": "error" if compiled else "warning",
                              "message": f"Module '{n}' is also defined in {other.name} - "
                                         + ("both are compiled, iverilog will fail with 'already declared'; rename one"
                                            if compiled else "rename one of them (iverilog fails with 'already declared' "
                                                             "if both files are ever compiled together)")})
            elif n in pk:
                diags.append({"file": str(path), "line": m["line"], "col": 0, "source": "registry", "code": "shadows-package",
                              "severity": "info",
                              "message": f"Module '{n}' also exists in package '{pk[n]['package_name']}' "
                                         f"({Path(pk[n]['filepath']).name}) - your definition is used, the package copy is masked"})
        return diags

    # ------------------------------------------------------------------ indexing & resolution
    def _lib_specs(self, enabled_only: bool = True) -> list[dict]:
        return [{"path": p["path"], "name": p["name"], "recursive": p["kind"] == "linked"}
                for p in self.packages() if p["exists"] and (p["enabled"] or not enabled_only)]

    def build_index(self, work_dir=None, exclude_files: list | None = None) -> dict[str, dict]:
        return build_global_module_index(self._lib_specs(), work_dir, self._cache, exclude_files)

    def modules_by_package(self) -> list[tuple[dict, list[dict]]]:
        """[(package, [module entries defined in it])] for every registered package (enabled or not)."""
        out = []
        for p in self.packages():
            idx = build_global_module_index([{"path": p["path"], "name": p["name"], "recursive": p["kind"] == "linked"}],
                                            None, self._cache) if p["exists"] else {}
            out.append((p, sorted(idx.values(), key=lambda m: m["module_name"].lower())))
        return out

    def stats(self) -> tuple[int, int]:
        active = [p for p in self.packages() if p["enabled"] and p["exists"]]
        return len(active), len(self.build_index())

    def resolve(self, work_dir, module: str, extra_files: list | tuple = ()) -> dict:
        """Everything the compiler needs for <module>.v + <module>_tb.v compiled from work_dir."""
        work_dir = Path(work_dir)
        roots = [work_dir / f"{module}.v", work_dir / f"{module}_tb.v"] + [work_dir / f for f in extra_files]
        index = self.build_index(work_dir, exclude_files=roots)
        shadowed: dict = {}
        y_dirs, l_files, missing = resolve_required_library_files(roots, index, shadowed)
        final_l, notes = [], []
        for lf in l_files:
            if lf in shadowed:
                final_l.append(str(self._filtered_copy(Path(lf), shadowed[lf])))
                notes.append(f"{Path(lf).name}: skipped {', '.join(sorted(shadowed[lf]))} (your own definition wins)")
            else:
                final_l.append(lf)
        return {"y_dirs": y_dirs, "l_files": final_l, "missing": missing, "notes": notes,
                "enabled_dirs": [str(p) for p in self.cfg.get_all_enabled_library_dirs()]}

    def _filtered_copy(self, src: Path, exclude: set[str]) -> Path:
        cache_dir = self.vault / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        tag = hashlib.sha1((os.path.normcase(str(src)) + "|" + ",".join(sorted(exclude))).encode()).hexdigest()[:10]
        target = cache_dir / f"{src.stem}.{tag}{src.suffix}"
        text = strip_modules_from_source(src.read_text(encoding="utf-8", errors="replace"), exclude)
        if not target.is_file() or target.read_text(encoding="utf-8", errors="replace") != text:
            target.write_text(text, encoding="utf-8")
        return target

    def open_vault_folder(self) -> Path:
        self.vault.mkdir(parents=True, exist_ok=True)
        return self.vault


__all__ = ["PackageManager", "DEFAULT_IMPORT_PACKAGE", "BUILTIN_PACKAGE_NAME", "safe_name"]
