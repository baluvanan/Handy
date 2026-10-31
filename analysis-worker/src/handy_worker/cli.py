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
from handy_worker import config as config_mod
from handy_worker.paths import ConfigError, Paths, resolve

#: Commands allowed to touch the network (the pinned model download only).
NETWORK_COMMANDS = {("model", "download"), ("spike", None)}

# Exit codes Handy relies on.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3  # needs attention (schema, model, environment)


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


def _config(paths: Paths) -> config_mod.WorkerConfig:
    return config_mod.load(paths.config_file)


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


# -- service -----------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from handy_worker import runner, system

    paths = _paths(args)
    system.setup_logging(paths.log_file, args.verbose, to_stderr=not args.quiet)
    cfg = _config(paths)
    system.lower_priority()
    name = args.transcriber or cfg.transcriber
    r = runner.Runner(
        paths,
        cfg,
        lambda: _make_transcriber(name, cfg.language, paths),
        notifier=runner.file_notifier(paths.identity.product_name),
        install_signal_handlers=True,
    )
    result = r.run(args.trigger)
    if args.json:
        _print_json(result.as_dict())
    if result.outcome == runner.BLOCKED:
        return EXIT_BLOCKED
    if result.outcome == runner.ERROR:
        return EXIT_FAILED
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    from handy_worker import status

    paths = _paths(args)
    _print_json(status.collect(paths, _config(paths)))
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    from handy_worker import launchd, system

    paths = _paths(args)
    system.setup_logging(paths.log_file, args.verbose)
    cfg = _config(paths)
    try:
        info = launchd.install(paths, cfg, Path(args.python) if args.python else None)
    except launchd.LaunchdError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAILED
    logging.getLogger(__name__).info("Installed launchd agent %s", info["label"])
    _print_json(info)
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    from handy_worker import launchd

    paths = _paths(args)
    _print_json(launchd.uninstall(paths, purge=args.purge))
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from handy_worker import doctor

    paths = _paths(args)
    checks = doctor.run_checks(paths, _config(paths), deep=not args.quick)
    if args.json:
        _print_json(doctor.as_json(checks))
    else:
        print(doctor.render(checks))
    return EXIT_OK if all(c.status != doctor.FAIL for c in checks) else EXIT_FAILED


def cmd_selftest(args: argparse.Namespace) -> int:
    from handy_worker import doctor

    paths = _paths(args)
    problems = doctor.selftest(paths, _config(paths))
    for p in problems:
        print(f"selftest: {p}", file=sys.stderr)
    if not problems:
        print("selftest: ok")
    return EXIT_OK if not problems else EXIT_FAILED


def cmd_retry(args: argparse.Namespace) -> int:
    from handy_worker import results_db

    paths = _paths(args)
    db = results_db.ResultsDb.open(paths.results_db)
    try:
        n = db.retry_all_failed() if args.all_failed else int(db.retry(args.entry_id))
    finally:
        db.close()
    _print_json({"requeued": n})
    return EXIT_OK if n or args.all_failed else EXIT_FAILED


def cmd_mark_synced(args: argparse.Namespace) -> int:
    from handy_worker import versions

    _print_json(versions.mark_synced(extras=args.extra or []))
    return EXIT_OK


def cmd_versions(args: argparse.Namespace) -> int:
    from handy_worker import versions

    _print_json(versions.report())
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

    r = sub.add_parser("run", help="process the queue (idempotent)")
    r.add_argument(
        "--trigger",
        choices=["launchd", "schedule", "login", "idle", "manual"],
        default="manual",
    )
    r.add_argument("--transcriber", choices=transcribers.NAMES, help="override config")
    r.add_argument("--json", action="store_true", help="print the run result as JSON")
    r.add_argument("--quiet", action="store_true", help="log to the file only")
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="print JSON status (last run, queue, failures, ...)")
    st.set_defaults(func=cmd_status)

    i = sub.add_parser("install", help="install/refresh the launchd agent")
    i.add_argument("--python", help="interpreter for the agent (default: this checkout's .venv)")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="remove the launchd agent (keeps data)")
    u.add_argument(
        "--purge",
        action="store_true",
        help="also delete the worker's own data (results, model, logs); never Handy's",
    )
    u.set_defaults(func=cmd_uninstall)

    d = sub.add_parser("doctor", help="check everything the service needs, with fixes")
    d.add_argument("--json", action="store_true")
    d.add_argument("--quick", action="store_true", help="skip importing the model stack")
    d.set_defaults(func=cmd_doctor)

    t = sub.add_parser("selftest", help=argparse.SUPPRESS)
    t.set_defaults(func=cmd_selftest)

    rt = sub.add_parser("retry", help="re-queue a failed entry, or all of them")
    g = rt.add_mutually_exclusive_group(required=True)
    g.add_argument("entry_id", nargs="?", type=int, help="dictation-log entry id")
    g.add_argument("--all-failed", action="store_true")
    rt.set_defaults(func=cmd_retry)

    ms = sub.add_parser("mark-synced", help=argparse.SUPPRESS)
    ms.add_argument("--extra", action="append")
    ms.set_defaults(func=cmd_mark_synced)

    v = sub.add_parser("versions", help="worker/environment versions as JSON")
    v.set_defaults(func=cmd_versions)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sub = getattr(args, "model_command", None) if args.command == "model" else None
    _privacy_env((args.command, sub) in NETWORK_COMMANDS)
    if args.command not in ("run", "install"):
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
