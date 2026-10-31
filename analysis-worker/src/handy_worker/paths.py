"""Where Handy's data lives, and where the worker keeps its own.

The bundle identifier and product name are read from the fork's
``src-tauri/tauri.conf.json`` (this package lives next to it in the repo),
never hard-coded. Handy passes ``HANDY_APP_DATA_DIR`` (and the launchd plist
bakes it in), which wins over the derived path, e.g. in portable mode.

Layout inside Handy's app-data folder::

    history.db                Handy's (read-only for the worker)
    recordings/, dataset/     Handy's audio (read-only for the worker)
    handy-busy.json           Handy's "recording active" flag (read-only)
    verbatim/                 the worker's own folder
        verbatim.db           results, queue state, run history
        config.json           run settings, written by Handy
        worker.lock           single-run lock (PID inside)
        models/               pinned model snapshots (Hugging Face cache)
        model-pin.json        the exact model revision in use
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
#: ``analysis-worker/`` (contains pyproject.toml and uv.lock).
WORKER_DIR = PACKAGE_DIR.parent.parent
#: The fork's checkout.
REPO_DIR = WORKER_DIR.parent
TAURI_CONF = REPO_DIR / "src-tauri" / "tauri.conf.json"


class ConfigError(RuntimeError):
    """The app identity or folders cannot be determined."""


@dataclass(frozen=True)
class AppIdentity:
    identifier: str
    product_name: str


def read_identity(conf: Path = TAURI_CONF) -> AppIdentity:
    """Bundle identifier and product name from tauri.conf.json, with env
    overrides (``HANDY_BUNDLE_ID`` / ``HANDY_PRODUCT_NAME``)."""
    identifier = os.environ.get("HANDY_BUNDLE_ID")
    product = os.environ.get("HANDY_PRODUCT_NAME")
    if not (identifier and product):
        try:
            data = json.loads(conf.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            if identifier:
                return AppIdentity(identifier, product or identifier)
            raise ConfigError(
                f"Cannot read the app identifier from {conf} ({e}). "
                "Run the worker from the fork's checkout or set HANDY_BUNDLE_ID."
            ) from e
        identifier = identifier or data.get("identifier")
        product = product or data.get("productName") or identifier
    if not identifier:
        raise ConfigError(f"No 'identifier' in {conf}")
    return AppIdentity(identifier, product or identifier)


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def default_app_data_dir(identity: AppIdentity) -> Path:
    """Tauri's ``app_data_dir`` for this identifier."""
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / identity.identifier
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else _home() / ".local" / "share"
    return base / identity.identifier


def default_log_dir(identity: AppIdentity) -> Path:
    """``~/Library/Logs/<product name>/`` on macOS."""
    if sys.platform == "darwin":
        return _home() / "Library" / "Logs" / identity.product_name
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else _home() / ".local" / "state"
    return base / identity.identifier / "logs"


@dataclass(frozen=True)
class Paths:
    identity: AppIdentity
    app_data: Path
    log_dir: Path

    @property
    def history_db(self) -> Path:
        return self.app_data / "history.db"

    @property
    def recordings_dir(self) -> Path:
        return self.app_data / "recordings"

    @property
    def dataset_audio_dir(self) -> Path:
        return self.app_data / "dataset" / "audio"

    @property
    def busy_flag(self) -> Path:
        return self.app_data / "handy-busy.json"

    @property
    def worker_data(self) -> Path:
        return self.app_data / "verbatim"

    @property
    def results_db(self) -> Path:
        return self.worker_data / "verbatim.db"

    @property
    def config_file(self) -> Path:
        return self.worker_data / "config.json"

    @property
    def lock_file(self) -> Path:
        return self.worker_data / "worker.lock"

    @property
    def models_dir(self) -> Path:
        return self.worker_data / "models"

    @property
    def model_pin(self) -> Path:
        return self.worker_data / "model-pin.json"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "verbatim-worker.log"

    @property
    def launchd_label(self) -> str:
        return f"{self.identity.identifier}.verbatim-worker"

    @property
    def launch_agent_plist(self) -> Path:
        return _home() / "Library" / "LaunchAgents" / f"{self.launchd_label}.plist"


def resolve(app_data: Path | None = None, log_dir: Path | None = None) -> Paths:
    """Paths from arguments, then ``HANDY_APP_DATA_DIR`` /
    ``HANDY_WORKER_LOG_DIR``, then the platform defaults."""
    identity = read_identity()
    env_data = os.environ.get("HANDY_APP_DATA_DIR")
    env_logs = os.environ.get("HANDY_WORKER_LOG_DIR")
    data = app_data or (Path(env_data) if env_data else default_app_data_dir(identity))
    logs = log_dir or (Path(env_logs) if env_logs else default_log_dir(identity))
    return Paths(identity=identity, app_data=data.expanduser(), log_dir=logs.expanduser())
