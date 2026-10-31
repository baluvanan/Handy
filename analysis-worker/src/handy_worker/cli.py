"""``handy-worker`` command line.

Every command except ``model download`` (and a ``spike`` that has to fetch
the model) runs fully offline: ``HF_HUB_OFFLINE=1`` is set before any
Hugging Face library is imported. No telemetry, ever.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from handy_worker import __version__, transcribers
from handy_worker.paths import ConfigError, Paths, resolve

#: Commands allowed to touch the network (the pinned model download only).
NETWORK_COMMANDS = {("model", "download"), ("spike", None)}

# Exit codes Handy relies on.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _privacy_env(allow_download: bool) -> None:
    """No telemetry, ever; offline unless this command may download the
    model. Must run before huggingface_hub/transformers are imported."""
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if allow_download:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
    else:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _paths(args: argparse.Namespace) -> Paths:
    return resolve(
        app_data=Path(args.app_data) if args.app_data else None,
        log_dir=Path(args.log_dir) if args.log_dir else None,
    )


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _make_transcriber(name: str, language: str, paths: Paths) -> transcribers.Transcriber:
    return transcribers.create(name, paths.models_dir, paths.model_pin, language=language)


# -- spike / model -----------------------------------------------------------


def cmd_spike(args: argparse.Namespace) -> int:
    from handy_worker import spike

    paths = _paths(args)
    target = args.target
    if target.isdigit():
        items = spike.items_from_history(paths, int(target))
        source = f"the {target} most recent dictations in {paths.history_db}"
    else:
        folder = Path(target).expanduser()
        if not folder.is_dir():
            print(f"error: {folder} is not a folder or a count", file=sys.stderr)
            return EXIT_USAGE
        items = spike.items_from_folder(folder, paths)
        source = str(folder)
    if not items:
        print(f"No recordings with audio found in {source}.", file=sys.stderr)
        return EXIT_FAILED
    transcriber = _make_transcriber(args.transcriber, args.language, paths)
    if not transcriber.is_available():
        if args.no_download or args.transcriber != "crisperwhisper":
            print(
                "error: the model is not downloaded (handy-worker model download)",
                file=sys.stderr,
            )
            return EXIT_FAILED
        from handy_worker.transcribers import crisper

        print(
            f"Downloading {crisper.MODEL_ID} once (licence: {crisper.LICENSE}) ...",
            file=sys.stderr,
        )
        record = crisper.download_model(paths.models_dir, paths.model_pin)
        print(f"Pinned revision {record['revision']}", file=sys.stderr)
    if not args.json:
        print(f"Spike over {source}: {len(items)} recording(s)", file=sys.stderr)
    color = sys.stdout.isatty() and not args.no_color
    spike.run_spike(transcriber, items, sys.stdout, color=color, as_json=args.json)
    return EXIT_OK


def cmd_model(args: argparse.Namespace) -> int:
    from handy_worker.transcribers import crisper

    paths = _paths(args)
    if args.model_command == "download":
        record = crisper.download_model(paths.models_dir, paths.model_pin, revision=args.revision)
        _print_json(record)
        return EXIT_OK
    _print_json(crisper.model_state(paths.models_dir, paths.model_pin))
    return EXIT_OK


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handy-worker",
        description="Handy Personal's local verbatim-analysis worker.",
    )
    p.add_argument("--version", action="version", version=f"handy-worker {__version__}")
    p.add_argument("--app-data", help="Handy's app-data folder (default: from tauri.conf.json)")
    p.add_argument("--log-dir", help="log folder (default: ~/Library/Logs/<app name>)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("spike", help="compare the verbatim model with Parakeet on a few recordings")
    s.add_argument(
        "target",
        nargs="?",
        default="5",
        help="a count of recent dictations (default 5) or a folder of .wav/.flac files",
    )
    s.add_argument("--transcriber", choices=transcribers.NAMES, default=transcribers.DEFAULT)
    s.add_argument("--language", default="en")
    s.add_argument("--no-download", action="store_true", help="fail instead of downloading")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--no-color", action="store_true")
    s.set_defaults(func=cmd_spike)

    m = sub.add_parser("model", help="download or inspect the pinned verbatim model")
    msub = m.add_subparsers(dest="model_command", required=True)
    md = msub.add_parser("download", help="download the model once (the only network access)")
    md.add_argument("--revision", help="pin this commit instead of the recorded/default one")
    msub.add_parser("status", help="show what is on disk")
    m.set_defaults(func=cmd_model)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sub = getattr(args, "model_command", None) if args.command == "model" else None
    _privacy_env((args.command, sub) in NETWORK_COMMANDS)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return int(args.func(args))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
