"""A thin wrapper over the official Kaggle Python API (kaggle>=2.2).

Everything kship asks of Kaggle goes through the `Kaggle` class, which keeps the
rest of the code testable with a fake API object.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from kship import KshipError

AUTH_HELP = """\
Kaggle doesn't know who you are yet. Pick one:
  kaggle auth login                     # easiest: browser sign-in, nothing to copy
  or create a token at https://www.kaggle.com/settings/api  ->  "Generate New Token"
     and save it:  printf '%s' 'KGAT_...' > ~/.kaggle/access_token && chmod 600 ~/.kaggle/access_token"""


@dataclass
class Source:
    label: str
    path: Path | None = None
    loose_permissions: bool = False


def credential_sources() -> list[Source]:
    """Every place the Kaggle client looks for credentials, in its lookup order."""
    home = Path.home() / ".kaggle"
    cfg_dir = Path(os.environ["KAGGLE_CONFIG_DIR"]) if os.environ.get("KAGGLE_CONFIG_DIR") else home
    found: list[Source] = []

    def file_source(label: str, p: Path) -> None:
        if p.is_file():
            loose = bool(p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)) if os.name == "posix" else False
            found.append(Source(label, p, loose))

    if os.environ.get("KAGGLE_API_TOKEN"):
        found.append(Source("KAGGLE_API_TOKEN environment variable"))
    file_source("access token file", home / "access_token")
    file_source("access token file", home / "access_token.txt")
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        found.append(Source("KAGGLE_USERNAME / KAGGLE_KEY environment variables (legacy)"))
    file_source("legacy API key", cfg_dir / "kaggle.json")
    file_source("OAuth login from `kaggle auth login`", home / "credentials.json")
    return found


def explain(e: Exception) -> str:
    """One readable line for an error raised by the Kaggle client or the network."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if code == 401:
        return "Kaggle rejected your credentials (401). Run `kaggle auth login` or make a new token."
    if code == 403:
        return "Kaggle said forbidden (403). Usually you haven't accepted this competition's rules yet."
    if code == 404:
        return "Kaggle couldn't find that (404). Check the competition slug."
    if code == 429:
        return "Kaggle is rate-limiting you (429). Wait a minute and retry."
    text = str(e).strip().splitlines()[0] if str(e).strip() else ""
    if len(text) > 160:
        text = text[:157] + "..."
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _name(enum_or_str) -> str:
    return str(getattr(enum_or_str, "name", enum_or_str) or "").upper()


def _slug(ref: str | None) -> str:
    return (ref or "").rstrip("/").split("/")[-1]


@dataclass
class CompetitionInfo:
    slug: str
    title: str = ""
    deadline: str = ""
    metric: str = ""
    max_daily: int | None = None
    code_only: bool = False
    joined: bool | None = None
    closed: bool = False

    @property
    def url(self) -> str:
        return f"https://www.kaggle.com/competitions/{self.slug}"


@dataclass
class Limits:
    allowed_now: int
    today: int
    total: int


@dataclass
class Submission:
    ref: str
    status: str  # PENDING | COMPLETE | ERROR
    public: float | None
    private: float | None
    description: str = ""
    error: str = ""
    date: str = ""


def _iso(d) -> str:
    return d.isoformat(timespec="seconds") if hasattr(d, "isoformat") else str(d or "")


def _to_float(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


class Kaggle:
    def __init__(self, api=None, sleep=time.sleep):
        self._api = api
        self._sleep = sleep

    # -- connection -----------------------------------------------------------

    @property
    def api(self):
        if self._api is None:
            self._api = self._connect()
        return self._api

    @staticmethod
    def _connect():
        noise = io.StringIO()
        # Importing kaggle authenticates eagerly and prints help on failure; keep that quiet.
        with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
            try:
                from kaggle.api.kaggle_api_extended import KaggleApi
            except ImportError:
                raise KshipError("the `kaggle` package isn't installed in kship's environment") from None
            api = KaggleApi()
            try:
                api.authenticate()
            except SystemExit:
                raise KshipError(AUTH_HELP) from None
            except Exception as e:
                raise KshipError(f"couldn't reach Kaggle to check your credentials: {explain(e)}") from None
        return api

    def whoami(self) -> tuple[str, str]:
        cv = getattr(self.api, "config_values", {}) or {}
        return cv.get("username", "?"), str(cv.get("auth_method", "") or "").replace("AuthMethod.", "").lower()

    # -- competitions ---------------------------------------------------------

    def competition(self, slug: str) -> CompetitionInfo | None:
        """Metadata for a competition, or None if search can't see it (common for private class competitions)."""
        for group in (None, "entered"):
            try:
                resp = self.api.competitions_list(group=group, search=slug, category="all", page_size=50)
            except Exception:
                continue
            for c in getattr(resp, "competitions", None) or []:
                if c is not None and _slug(c.ref) == slug:
                    deadline = c.deadline
                    return CompetitionInfo(
                        slug=slug,
                        title=c.title or "",
                        deadline=(
                            deadline.strftime("%Y-%m-%d %H:%M UTC")
                            if hasattr(deadline, "strftime")
                            else str(deadline or "")
                        ),
                        metric=c.evaluation_metric or "",
                        max_daily=c.max_daily_submissions or None,
                        code_only=bool(c.is_kernels_submissions_only),
                        joined=bool(c.user_has_entered),
                        closed=bool(c.submissions_disabled),
                    )
        return None

    def limits(self, slug: str) -> Limits | None:
        try:
            r = self.api.competition_get_submission_limits(slug)
        except Exception:
            return None
        return Limits(int(r.num_allowed_now or 0), int(r.num_today or 0), int(r.num_total or 0))

    def files(self, slug: str) -> list[str]:
        resp = self.api.competition_list_files(slug, page_size=200)
        return [f.name for f in (getattr(resp, "files", None) or []) if f is not None]

    def download_file(self, slug: str, name: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        self.api.competition_download_file(slug, name, path=str(dest), force=True, quiet=True)
        target = dest / Path(name).name
        zipped = dest / (Path(name).name + ".zip")
        if not target.exists() and zipped.exists():
            with zipfile.ZipFile(zipped) as z:
                z.extractall(dest)
            zipped.unlink()
        if not target.exists():
            raise KshipError(f"downloaded {name} but can't find it in {dest}")
        return target

    def download_all(self, slug: str, dest: Path) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        self.api.competition_download_files(slug, path=str(dest), force=True, quiet=False)
        archive = dest / f"{slug}.zip"
        if archive.exists():
            staging = dest / ".kship-unzip"
            shutil.rmtree(staging, ignore_errors=True)
            with zipfile.ZipFile(archive) as z:
                z.extractall(staging)
            archive.unlink()
            # Some competitions wrap everything in one folder; unwrap it so paths match the sample's.
            top = [p for p in staging.iterdir() if p.name not in ("__MACOSX", ".DS_Store")]
            root = top[0] if len(top) == 1 and top[0].is_dir() else staging
            for item in root.iterdir():
                target = dest / item.name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                shutil.move(str(item), str(target))
            shutil.rmtree(staging, ignore_errors=True)
        return sorted(p for p in dest.rglob("*") if p.is_file())

    # -- submitting -----------------------------------------------------------

    def submit_file(self, slug: str, path: Path, message: str) -> str | None:
        resp = self.api.competition_submit(str(path), message, slug, quiet=True)
        failed = getattr(self.api, "COMPETITION_SUBMIT_UPLOAD_FAILED_MESSAGE", "Could not submit to competition")
        if getattr(resp, "message", "") == failed:
            raise KshipError("Kaggle rejected the upload (see the message above)")
        ref = getattr(resp, "ref", None)
        return str(ref) if ref else None

    def push_kernel(self, folder: Path) -> tuple[str, int]:
        meta_path = folder / "kernel-metadata.json"
        if not meta_path.is_file():
            raise KshipError(f"{meta_path} is missing (scaffold one with `kaggle kernels init -p {folder}`)")
        kernel_id = json.loads(meta_path.read_text()).get("id", "")
        if kernel_id.count("/") != 1:
            raise KshipError(f"{meta_path}: id must look like <your-username>/<notebook-slug>")
        resp = self.api.kernels_push(str(folder))
        if getattr(resp, "error", None):
            raise KshipError(f"kernel push failed: {resp.error}")
        return kernel_id, int(resp.version_number)

    def wait_kernel(self, kernel_id: str, timeout_s: float, interval: float = 30, on_tick=None) -> None:
        start = time.monotonic()
        while True:
            r = self.api.kernels_status(kernel_id)
            status = _name(getattr(r, "status", ""))
            if status == "COMPLETE":
                return
            if status in ("ERROR", "CANCEL_REQUESTED", "CANCEL_ACKNOWLEDGED"):
                raise KshipError(f"kernel run ended with {status}: {getattr(r, 'failure_message', '') or 'no details'}")
            if time.monotonic() - start > timeout_s:
                raise KshipError(f"kernel still {status} after {timeout_s / 60:.0f} min; check it on Kaggle")
            if on_tick:
                on_tick(status, time.monotonic() - start)
            self._sleep(interval)

    def submit_kernel(self, slug: str, kernel_id: str, version: int, output_file: str, message: str) -> str | None:
        resp = self.api.competition_submit_code(output_file, message, slug, kernel_id, version, quiet=True)
        ref = getattr(resp, "ref", None)
        return str(ref) if ref else None

    # -- scores ---------------------------------------------------------------

    def submissions(self, slug: str, page_size: int = 50) -> list[Submission]:
        subs = self.api.competition_submissions(slug, page_size=page_size) or []
        return [
            Submission(
                ref=str(s.ref),
                status=_name(s.status) or "PENDING",
                public=_to_float(s.public_score),
                private=_to_float(s.private_score),
                description=s.description or "",
                error=getattr(s, "error_description", "") or "",
                date=_iso(getattr(s, "date", None)),
            )
            for s in subs
            if s is not None
        ]

    def wait_for_score(
        self, slug: str, ref: str | None, message: str, timeout_s: float, interval: float = 15
    ) -> Submission | None:
        """Poll until the submission leaves PENDING or the timeout passes; returns the latest view of it."""
        start = time.monotonic()
        latest = None
        while True:
            for s in self.submissions(slug, page_size=20):
                if (ref and s.ref == ref) or (not ref and s.description == message):
                    latest = s
                    break
            if latest and latest.status != "PENDING":
                return latest
            if time.monotonic() - start >= timeout_s:
                return latest
            self._sleep(interval)
