"""Per-project settings, stored in a kship.toml at the project root."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from kship import KshipError

CONFIG_NAME = "kship.toml"
LEDGER_NAME = "submissions.jsonl"
MARKDOWN_NAME = "SUBMISSIONS.md"
_SCHEME = re.compile(r"[A-Za-z0-9._-]*")

# Metric names whose score improves as it goes *down*. Anything else is assumed
# to improve upward, which covers AUC, accuracy, F1, MAP@k, Dice, kappa, etc.
_LOWER_IS_BETTER = (
    "rmse",
    "mse",
    "mae",
    "rmsle",
    "msle",
    "logloss",
    "log loss",
    "log_loss",
    "cross entropy",
    "crossentropy",
    "error",
    "loss",
    "smape",
    "mape",
    "wape",
    "brier",
    "pinball",
    "quantile",
    "levenshtein",
    "wer",
    "cer",
    "distance",
)


def guess_higher_is_better(metric: str | None) -> bool | None:
    """Best guess at a metric's direction from its name; None when there is no name."""
    if not metric:
        return None
    m = metric.lower()
    return not any(word in m for word in _LOWER_IS_BETTER)


@dataclass
class Config:
    competition: str
    root: Path = field(default_factory=Path.cwd)
    mode: str = "file"  # "file" uploads a CSV; "notebook" pushes a kernel (code competitions)
    submission_dir: str = "submissions"
    sample_submission: str = ""
    metric: str = ""
    higher_is_better: bool | None = None
    min_improvement: float = 0.0
    cv_scheme: str = ""  # bumped when CV is computed differently; the gate only compares within a scheme
    reserve: int = 1  # daily slots that automated runs (`kship run`) leave for you
    poll_minutes: float = 10.0
    value_range: tuple[float, float] | None = None
    kernel_dir: str = "kernel"
    kernel_output: str = "submission.csv"
    kernel_timeout_minutes: float = 120.0

    @property
    def ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    @property
    def submissions_path(self) -> Path:
        return self.root / self.submission_dir

    @property
    def sample_path(self) -> Path | None:
        return self.root / self.sample_submission if self.sample_submission else None


def find_config(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file():
            return d / CONFIG_NAME
    return None


def load(start: Path | None = None) -> Config:
    path = find_config(start)
    if path is None:
        raise KshipError(
            f"no {CONFIG_NAME} found here or in any parent folder.\n  Set one up with:  kship init <competition-slug>"
        )
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise KshipError(f"{path}: {e}") from None

    if not data.get("competition"):
        raise KshipError(f"{path}: 'competition' is required")
    check = data.get("check", {})
    nb = data.get("notebook", {})
    rng = check.get("range")
    if rng is not None and (len(rng) != 2 or rng[0] > rng[1]):
        raise KshipError(f"{path}: [check] range must be [low, high]")
    mode = data.get("mode", "file")
    if mode not in ("file", "notebook"):
        raise KshipError(f'{path}: mode must be "file" or "notebook", got {mode!r}')
    scheme = str(data.get("cv_scheme", ""))
    if not _SCHEME.fullmatch(scheme):
        raise KshipError(f"{path}: cv_scheme can only use letters, digits, '.', '_' and '-' (it's tagged on Kaggle)")

    return Config(
        competition=data["competition"],
        root=path.parent,
        mode=mode,
        submission_dir=data.get("submission_dir", "submissions"),
        sample_submission=data.get("sample_submission", ""),
        metric=data.get("metric", ""),
        higher_is_better=data.get("higher_is_better"),
        min_improvement=float(data.get("min_improvement", 0.0)),
        cv_scheme=scheme,
        reserve=int(data.get("reserve", 1)),
        poll_minutes=float(data.get("poll_minutes", 10)),
        value_range=(float(rng[0]), float(rng[1])) if rng else None,
        kernel_dir=nb.get("kernel_dir", "kernel"),
        kernel_output=nb.get("output_file", "submission.csv"),
        kernel_timeout_minutes=float(nb.get("timeout_minutes", 120)),
    )


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(cfg: Config, *, title: str = "", deadline: str = "", daily: int | None = None) -> str:
    """The kship.toml text for a config, with comments explaining each knob."""
    hib = "true" if cfg.higher_is_better else "false"
    hib_line = (
        f"higher_is_better = {hib}"
        if cfg.higher_is_better is not None
        else "# higher_is_better = true   # set this: the CV gate needs to know which way is up"
    )
    header = f"# {title}\n" if title else ""
    facts = []
    if deadline:
        facts.append(f"deadline {deadline}")
    if daily:
        facts.append(f"{daily} submissions/day")
    if facts:
        header += f"# {', '.join(facts)}\n"
    rng = (
        f"range = [{cfg.value_range[0]:g}, {cfg.value_range[1]:g}]"
        if cfg.value_range
        else "# range = [0, 1]   # reject predictions outside this range"
    )
    return f"""{header}competition = {_toml_str(cfg.competition)}
mode = {_toml_str(cfg.mode)}            # "file" uploads a CSV, "notebook" pushes kernel/ (code competitions)

submission_dir = {_toml_str(cfg.submission_dir)}   # `kship run` ships the newest CSV your pipeline writes here
sample_submission = {_toml_str(cfg.sample_submission)}

metric = {_toml_str(cfg.metric)}
{hib_line}
min_improvement = {cfg.min_improvement:g}     # CV must beat your best by more than this to ship
cv_scheme = {_toml_str(cfg.cv_scheme)}            # change it (e.g. "v2") when you change how CV is computed:
                              # the gate then only compares against CVs from the same scheme

reserve = {cfg.reserve}               # daily slots automated runs leave untouched for you
poll_minutes = {cfg.poll_minutes:g}         # how long to wait for the leaderboard score

[check]
{rng}

[notebook]
kernel_dir = {_toml_str(cfg.kernel_dir)}
output_file = {_toml_str(cfg.kernel_output)}
timeout_minutes = {cfg.kernel_timeout_minutes:g}
"""
