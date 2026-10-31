"""A tiny, randomly initialised Whisper laid out like a CrisperWhisper
snapshot, to exercise the real transformers code path without a download.
Only importable when the model extra is installed."""

from __future__ import annotations

import json
from pathlib import Path

import torch
from transformers import WhisperConfig, WhisperFeatureExtractor, WhisperForConditionalGeneration
from transformers.models.gpt2.tokenization_gpt2 import bytes_to_unicode


def make_tiny_whisper(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    byte_chars = list(bytes_to_unicode().values())
    vocab = {c: i for i, c in enumerate(byte_chars)}
    # a few merges so words are multi-token
    merges = ["#version: 0.2", "Ġ t", "h e", "Ġt he", "u m", "Ġ um"]
    for m in merges[1:]:
        a, b = m.split()
        vocab.setdefault(a + b, len(vocab))
    specials = [
        "<|endoftext|>",
        "<|startoftranscript|>",
        "<|en|>",
        "<|de|>",
        "<|translate|>",
        "<|transcribe|>",
        "<|startoflm|>",
        "<|startofprev|>",
        "<|nospeech|>",
        "<|notimestamps|>",
    ]
    for s in specials:
        vocab[s] = len(vocab)
    ids = {s: vocab[s] for s in specials}
    vocab_size = len(vocab) + 1501
    (out / "vocab.json").write_text(json.dumps(vocab))
    (out / "merges.txt").write_text("\n".join(merges) + "\n")
    added = {
        str(ids[s]): {
            "content": s,
            "lstrip": False,
            "rstrip": False,
            "normalized": False,
            "single_word": False,
            "special": True,
        }
        for s in specials
    }
    (out / "tokenizer_config.json").write_text(
        json.dumps(
            {
                "tokenizer_class": "WhisperTokenizer",
                "added_tokens_decoder": added,
                "additional_special_tokens": specials[1:],
                "bos_token": "<|endoftext|>",
                "eos_token": "<|endoftext|>",
                "unk_token": "<|endoftext|>",
                "pad_token": "<|endoftext|>",
                "model_max_length": 1024,
                "errors": "replace",
            }
        )
    )
    (out / "special_tokens_map.json").write_text(
        json.dumps(
            {
                "additional_special_tokens": specials[1:],
                "bos_token": "<|endoftext|>",
                "eos_token": "<|endoftext|>",
                "unk_token": "<|endoftext|>",
                "pad_token": "<|endoftext|>",
            }
        )
    )
    cfg = WhisperConfig(
        vocab_size=vocab_size,
        num_mel_bins=80,
        encoder_layers=2,
        decoder_layers=2,
        encoder_attention_heads=2,
        decoder_attention_heads=2,
        d_model=64,
        encoder_ffn_dim=128,
        decoder_ffn_dim=128,
        max_source_positions=1500,
        max_target_positions=448,
        decoder_start_token_id=ids["<|startoftranscript|>"],
        eos_token_id=ids["<|endoftext|>"],
        bos_token_id=ids["<|endoftext|>"],
        pad_token_id=ids["<|endoftext|>"],
        suppress_tokens=[],
        begin_suppress_tokens=[ids["<|endoftext|>"]],
    )
    torch.manual_seed(0)
    model = WhisperForConditionalGeneration(cfg)
    model.save_pretrained(out, safe_serialization=True)
    gen = json.loads((out / "generation_config.json").read_text())
    gen.pop("_from_model_config", None)
    gen.update(
        {
            "alignment_heads": [[1, 0], [1, 1]],
            "no_timestamps_token_id": ids["<|notimestamps|>"],
            "lang_to_id": {"<|en|>": ids["<|en|>"], "<|de|>": ids["<|de|>"]},
            "task_to_id": {"transcribe": ids["<|transcribe|>"], "translate": ids["<|translate|>"]},
            "is_multilingual": True,
            "max_initial_timestamp_index": 50,
            "max_length": 40,
            "forced_decoder_ids": [[1, None], [2, ids["<|transcribe|>"]]],
            "decoder_start_token_id": ids["<|startoftranscript|>"],
            "suppress_tokens": [],
            "begin_suppress_tokens": [ids["<|endoftext|>"]],
            "token_ids_to_ignore_for_dtw": [ids["<|endoftext|>"]],
            "median_filter_width": 7,
            "legacy": True,
        }
    )
    (out / "generation_config.json").write_text(json.dumps(gen, indent=2))
    WhisperFeatureExtractor(feature_size=80).save_pretrained(out)
    pp = json.loads((out / "preprocessor_config.json").read_text())
    pp["processor_class"] = "WhisperProcessor"
    (out / "preprocessor_config.json").write_text(json.dumps(pp))
