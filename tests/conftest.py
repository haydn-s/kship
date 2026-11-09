from __future__ import annotations

import enum
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from kship import cli
from kship.kapi import Kaggle

SLUG = "demo-comp"
SAMPLE = "id,target\n1,0.5\n2,0.5\n3,0.5\n4,0.5\n"


class Status(enum.Enum):
    PENDING = 0
    COMPLETE = 1
    ERROR = 2


class KStatus(enum.Enum):
    QUEUED = 0
    RUNNING = 1
    COMPLETE = 2
    ERROR = 3


class FakeApi:
    """Mimics the parts of kaggle.api.kaggle_api_extended.KaggleApi that kship uses."""

    COMPETITION_SUBMIT_UPLOAD_FAILED_MESSAGE = "Could not submit to competition"

    def __init__(self):
        self.config_values = {"username": "haydn", "auth_method": "AuthMethod.OAUTH"}
        self.joined = True
        self.code_only = False
        self.metric = "AUC"
        self.allowed_now, self.today, self.total = 5, 0, 3
        self.uploads: list[tuple[str, str]] = []
        self.subs: list[NS] = []
        self.next_score = 0.9
        self.score_after_polls = 1
        self.polls = 0
        self.fail_score = False
        self.kernel_polls = 0
        self.kernel_pushes = 0
        self.code_submits: list[tuple] = []
        self.sample_name = "sample_submission.csv"  # class competitions often nest it: for_students/...
        self.zip_downloads = False  # Kaggle serves large files as <name>.zip
        self.ignore_paging = False
        self.nested_data = False  # `kship data`'s archive keeps the sample in a subfolder

    def competitions_list(self, group=None, search=None, category=None, page_size=20, **_):
        if group is not None:
            return NS(competitions=[])
        return NS(
            competitions=[
                NS(
                    ref=f"https://www.kaggle.com/competitions/{SLUG}",
                    title="Demo Competition",
                    deadline="2026-12-31",
                    evaluation_metric=self.metric,
                    max_daily_submissions=5,
                    is_kernels_submissions_only=self.code_only,
                    user_has_entered=self.joined,
                    submissions_disabled=False,
                )
            ]
        )

    def competition_get_submission_limits(self, slug):
        return NS(num_allowed_now=self.allowed_now, num_today=self.today, num_total=self.total)

    def competition_list_files(self, slug, page_size=20, **_):
        return NS(files=[NS(name="train.csv"), NS(name=self.sample_name)])

    def competition_download_file(self, slug, name, path=None, force=False, quiet=False):
        # Like the real client: keeps the file's folders, and doesn't unzip what it zipped.
        out = Path(path, name)
        out.parent.mkdir(parents=True, exist_ok=True)
        if self.zip_downloads:
            import zipfile

            with zipfile.ZipFile(out.with_name(out.name + ".zip"), "w") as z:
                z.writestr(out.name, SAMPLE)
        else:
            out.write_text(SAMPLE)

    def competition_download_files(self, slug, path=None, force=False, quiet=True):
        import zipfile

        with zipfile.ZipFile(Path(path, f"{slug}.zip"), "w") as z:  # wrapped in one folder, like many class comps
            z.writestr("for_students/train.csv", "id,x,target\n1,1,0\n")
            z.writestr("for_students/sample_submission.csv", SAMPLE)
            if self.nested_data:  # a second top-level entry, so the folder isn't unwrapped
                z.writestr("README.md", "about the data\n")

    def _new_sub(self, message):
        ref = str(1000 + len(self.subs))
        self.subs.insert(
            0,
            NS(
                ref=ref,
                status=Status.PENDING,
                public_score="",
                private_score="",
                description=message,
                error_description="",
                date="2026-11-04T15:00:00",
            ),
        )
        return ref

    def competition_submit(self, file_name, message, competition, quiet=False):
        self.uploads.append((file_name, message))
        return NS(message="Successfully submitted", ref=self._new_sub(message))

    def competition_submissions(self, competition, page_number=-1, page_size=20, **_):
        page = max(page_number, 1)  # like the real client: -1 means the first page
        if page == 1:
            self.polls += 1
            for s in self.subs:
                if s.status is Status.PENDING and self.polls >= self.score_after_polls:
                    if self.fail_score:
                        s.status, s.error_description = Status.ERROR, "bad file"
                    else:
                        s.status, s.public_score = Status.COMPLETE, str(self.next_score)
        if self.ignore_paging:  # a server that always answers with the same first page
            return list(self.subs[:page_size])
        return list(self.subs[(page - 1) * page_size : page * page_size])

    def kernels_push(self, folder, timeout=None, acc=None):
        self.kernel_pushes += 1
        return NS(error=None, version_number=7, ref="/code/haydn/demo")

    def kernels_status(self, kernel):
        self.kernel_polls += 1
        return NS(status=KStatus.COMPLETE if self.kernel_polls >= 2 else KStatus.RUNNING, failure_message="")

    def competition_submit_code(self, file_name, message, competition, kernel, kernel_version, quiet=False):
        self.code_submits.append((file_name, message, competition, kernel, kernel_version))
        return NS(message="ok", ref=self._new_sub(message))


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeApi()
    monkeypatch.setattr(cli, "make_kaggle", lambda: Kaggle(api=fake, sleep=lambda s: None))
    monkeypatch.setattr(cli, "remote_has_tag", lambda url, tag: True)  # no network in tests
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KSHIP_NO_NOTIFY", "1")
    # GitHub runners set these; tests that want CI behaviour set GITHUB_ACTIONS themselves.
    for var in (
        "KAGGLE_API_TOKEN",
        "KAGGLE_USERNAME",
        "KAGGLE_KEY",
        "KAGGLE_CONFIG_DIR",
        "GITHUB_ACTIONS",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_OUTPUT",
        "KSHIP_SUMMARY_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "home").mkdir()
    return fake


@pytest.fixture
def project(api, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    assert cli.main(["init", SLUG]) == 0
    return root


def write_sub(root: Path, name: str, values=(0.1, 0.2, 0.3, 0.4), ids=(1, 2, 3, 4)) -> Path:
    d = root / "submissions"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("id,target\n" + "".join(f"{i},{v}\n" for i, v in zip(ids, values, strict=True)))
    return p
