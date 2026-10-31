"""CrisperWhisper (``nyrahealth/CrisperWhisper``): verbatim Whisper that
keeps fillers, false starts and repetitions, with accurate word timings.

Licence: the weights are CC BY-NC 4.0 (non-commercial). They are never
bundled or redistributed: ``download_model`` fetches them at runtime, once,
into Handy's app-data folder, pinned to an exact revision.

Implementation follows the model card: the ``crisper_whisper`` branch of
nyrahealth's transformers fork (pinned in pyproject.toml) for its
timestamp/DTW changes, word timestamps from the cross-attention DTW, and the
card's ``adjust_pauses_for_hf_pipeline_output`` post-processing. Instead of
the pipeline we call ``generate`` per <=30 s chunk ourselves, so that

- a ``StoppingCriteria`` polls the "stop" check at every decoding step
  (a sub-second yield when dictation starts), and
- token probabilities are available for per-word confidences.
"""

from __future__ import annotations

import gc
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

from handy_worker.audio import Audio
from handy_worker.transcribers.base import (
    ChunkResult,
    ModelInfo,
    ModelUnavailableError,
    StopCheck,
    Word,
    Yielded,
)

log = logging.getLogger(__name__)

MODEL_ID = "nyrahealth/CrisperWhisper"
#: Set this to a commit sha to pin the model before the first download. When
#: None, the first ``model download`` resolves ``main`` to its current commit
#: and records it in ``model-pin.json``; every later load uses exactly that
#: commit, offline. (huggingface.co could not be reached when this was
#: written, so the sha could not be filled in here.)
PINNED_REVISION: str | None = None
#: Files needed for inference (no PyTorch .bin duplicates, no flax/tf).
ALLOW_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.model"]
LICENSE = "CC BY-NC 4.0 (non-commercial use only)"
#: From the model card.
PAUSE_SPLIT_THRESHOLD = 0.12
HOP_LENGTH = 160


def repo_cache_dir(models_dir: Path, model_id: str = MODEL_ID) -> Path:
    return models_dir / ("models--" + model_id.replace("/", "--"))


def snapshot_dir(models_dir: Path, revision: str, model_id: str = MODEL_ID) -> Path:
    return repo_cache_dir(models_dir, model_id) / "snapshots" / revision


def read_pin(pin_file: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(pin_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("revision") else None


def snapshot_complete(path: Path) -> bool:
    return (
        (path / "config.json").is_file()
        and (path / "preprocessor_config.json").is_file()
        and any(path.glob("*.safetensors"))
    )


def model_state(models_dir: Path, pin_file: Path) -> dict[str, Any]:
    """For ``status``/``doctor``: what is on disk, without loading anything."""
    pin = read_pin(pin_file)
    if not pin:
        return {"model_id": MODEL_ID, "state": "not_downloaded", "license": LICENSE}
    snap = snapshot_dir(models_dir, str(pin["revision"]))
    size = sum(p.stat().st_size for p in snap.rglob("*") if p.is_file()) if snap.exists() else 0
    return {
        "model_id": MODEL_ID,
        "revision": pin["revision"],
        "state": "ready" if snapshot_complete(snap) else "missing_files",
        "path": str(snap),
        "bytes": size,
        "license": LICENSE,
    }


def adjust_pauses(words: list[Word], split_threshold: float = PAUSE_SPLIT_THRESHOLD) -> list[Word]:
    """The model card's ``adjust_pauses_for_hf_pipeline_output``: give each
    word up to half of ``split_threshold`` of the pause on either side."""
    out = list(words)
    for i in range(len(out) - 1):
        cur, nxt = out[i], out[i + 1]
        pause = nxt.start - cur.end
        if pause > 0:
            distribute = split_threshold / 2 if pause > split_threshold else pause / 2
            out[i] = Word(cur.text, cur.start, cur.end + distribute, cur.confidence)
            out[i + 1] = Word(nxt.text, nxt.start - distribute, nxt.end, nxt.confidence)
    return out


def download_model(models_dir: Path, pin_file: Path, revision: str | None = None) -> dict[str, Any]:
    """The only network access the worker ever makes: fetch the pinned
    snapshot once. Requires ``HF_HUB_OFFLINE`` to be unset at import time
    (the CLI arranges that for this command only)."""
    from huggingface_hub import HfApi, snapshot_download

    pin = read_pin(pin_file)
    wanted = revision or PINNED_REVISION or (pin["revision"] if pin else None)
    if wanted is None:
        wanted = HfApi().model_info(MODEL_ID, revision="main").sha
        log.info("Pinning %s to the current main commit %s", MODEL_ID, wanted)
    if not wanted:
        raise ModelUnavailableError("could not resolve a model revision to pin")
    models_dir.mkdir(parents=True, exist_ok=True)
    path = Path(
        snapshot_download(
            MODEL_ID,
            revision=wanted,
            cache_dir=str(models_dir),
            allow_patterns=ALLOW_PATTERNS,
        )
    )
    if not snapshot_complete(path):
        raise ModelUnavailableError(f"download incomplete: {path}")
    record = {
        "model_id": MODEL_ID,
        "revision": wanted,
        "downloaded_at": int(time.time()),
        "license": LICENSE,
    }
    pin_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = pin_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(pin_file)
    return record


class CrisperWhisperTranscriber:
    name = "crisperwhisper"

    def __init__(
        self,
        models_dir: Path,
        pin_file: Path,
        language: str = "en",
        device: str | None = None,
    ) -> None:
        self.models_dir = models_dir
        self.pin_file = pin_file
        self.language = language
        self.requested_device = device
        self.device: str | None = None
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self._dtype: Any = None
        self._revision: str | None = None

    def _snapshot(self) -> Path | None:
        pin = read_pin(self.pin_file)
        if not pin:
            return None
        self._revision = str(pin["revision"])
        return snapshot_dir(self.models_dir, self._revision)

    def info(self) -> ModelInfo:
        if self._revision is None:
            self._snapshot()
        return ModelInfo(self.name, MODEL_ID, self._revision, self.device)

    def is_available(self) -> bool:
        snap = self._snapshot()
        return snap is not None and snapshot_complete(snap)

    def load(self) -> None:
        if self._model is not None:
            return
        snap = self._snapshot()
        if snap is None or not snapshot_complete(snap):
            raise ModelUnavailableError(
                f"{MODEL_ID} is not downloaded (run: handy-worker model download)"
            )
        # Must be set before torch is imported.
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        try:
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
        except ImportError as e:
            raise ModelUnavailableError(
                f"model stack not installed ({e}); run: uv sync --extra model"
            ) from e
        self._torch = torch
        device = self.requested_device
        if device is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        candidates = [device] if device == "cpu" else [device, "cpu"]
        last_error: Exception | None = None
        for dev in candidates:
            dtype = torch.float16 if dev != "cpu" else torch.float32
            try:
                model = AutoModelForSpeechSeq2Seq.from_pretrained(
                    str(snap),
                    torch_dtype=dtype,
                    low_cpu_mem_usage=True,
                    use_safetensors=True,
                    local_files_only=True,
                    # Word timestamps need cross-attention weights, which
                    # SDPA attention can't return.
                    attn_implementation="eager",
                )
                model.to(dev)
                model.eval()
                self._processor = AutoProcessor.from_pretrained(str(snap), local_files_only=True)
                self._model, self._dtype, self.device = model, dtype, dev
                log.info("Loaded %s@%s on %s", MODEL_ID, self._revision, dev)
                return
            except Exception as e:  # fall back to CPU on any MPS failure
                last_error = e
                log.warning("Loading on %s failed (%s); trying the next device", dev, e)
                self._release()
        raise ModelUnavailableError(f"cannot load {MODEL_ID}: {last_error}")

    def _release(self) -> None:
        self._model = None
        self._processor = None
        gc.collect()
        torch = self._torch
        if torch is not None and getattr(torch.backends, "mps", None) is not None:
            try:
                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            except Exception:  # pragma: no cover - best effort
                pass

    def unload(self) -> None:
        self._release()
        self.device = None

    def peak_gpu_bytes(self) -> int | None:
        torch = self._torch
        if torch is None or self.device != "mps":
            return None
        try:
            return int(torch.mps.driver_allocated_memory())
        except Exception:  # pragma: no cover
            return None

    def transcribe_chunk(self, audio: Audio, should_stop: StopCheck) -> ChunkResult:
        if self._model is None:
            raise RuntimeError("transcribe_chunk before load")
        torch = self._torch
        from transformers import StoppingCriteria, StoppingCriteriaList
        from transformers.models.whisper.tokenization_whisper import (
            _combine_tokens_into_words,
        )

        stopped = {"flag": False}

        class _Abort(StoppingCriteria):  # type: ignore[misc]
            def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> bool:
                if should_stop():
                    stopped["flag"] = True
                    return True
                return False

        if should_stop():
            raise Yielded()
        import numpy as np

        samples = np.frombuffer(audio.samples.tobytes(), dtype=np.float32)
        features = self._processor.feature_extractor(
            samples, sampling_rate=audio.sample_rate, return_tensors="pt"
        ).input_features.to(self.device, dtype=self._dtype)
        num_frames = len(samples) // HOP_LENGTH
        abort = StoppingCriteriaList([_Abort()])
        try:
            with torch.inference_mode():
                out = self._model.generate(
                    features,
                    language=self.language,
                    task="transcribe",
                    return_timestamps=True,
                    return_token_timestamps=True,
                    return_dict_in_generate=True,
                    output_scores=True,
                    num_frames=num_frames,
                    stopping_criteria=abort,
                )
        except Exception as e:
            if stopped["flag"] or should_stop():
                # Aborted mid-decode; the DTW step may choke on the stub.
                raise Yielded() from e
            # Word alignment failed on this chunk (seen with degenerate
            # outputs): keep the text, without word timings. Such a
            # transcript never verifies, so its audio is never deleted.
            log.warning("Word timestamps failed (%s); keeping text only", e)
            with torch.inference_mode():
                plain = self._model.generate(
                    features,
                    language=self.language,
                    task="transcribe",
                    stopping_criteria=abort,
                )
            if stopped["flag"] or should_stop():
                raise Yielded() from e
            tokens_only = [
                t for t in plain[0].tolist() if t < self._processor.tokenizer.eos_token_id
            ]
            text = self._processor.tokenizer.decode(tokens_only, skip_special_tokens=True)
            return ChunkResult(text=text.strip(), words=[])
        if stopped["flag"] or should_stop():
            raise Yielded()

        tokenizer = self._processor.tokenizer
        sequence: list[int] = out.sequences[0].tolist()
        stamps: list[float] = out.token_timestamps[0].tolist()
        scores = out.scores
        prompt_len = len(sequence) - len(scores)
        timestamp_begin = tokenizer.convert_tokens_to_ids("<|notimestamps|>") + 1
        special = set(tokenizer.all_special_ids)

        tokens: list[int] = []
        spans: list[tuple[float, float]] = []
        probs: list[float | None] = []
        for i, tok in enumerate(sequence):
            if tok in special or tok >= timestamp_begin:
                continue
            start = float(stamps[i])
            end = float(stamps[i + 1]) if i + 1 < len(stamps) else start
            step = i - prompt_len
            prob: float | None = None
            if 0 <= step < len(scores):
                logprobs = torch.log_softmax(scores[step][0].float(), dim=-1)
                prob = float(math.exp(float(logprobs[tok])))
            tokens.append(tok)
            spans.append((start, end))
            probs.append(prob)

        text = tokenizer.decode(tokens, skip_special_tokens=True).strip()
        if not tokens:
            return ChunkResult(text="", words=[])
        words_txt, _, indices = _combine_tokens_into_words(tokenizer, tokens, "english")
        words: list[Word] = []
        for w, idx in zip(words_txt, indices, strict=False):
            if not idx or w.strip() == "":
                continue
            p = [v for v in (probs[k] for k in idx) if v is not None]
            conf = sum(p) / len(p) if p else None
            words.append(Word(w.strip(), spans[idx[0]][0], spans[idx[-1]][1], conf))
        return ChunkResult(text=text, words=adjust_pauses(words))
