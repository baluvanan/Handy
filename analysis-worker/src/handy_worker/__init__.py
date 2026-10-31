"""Handy Personal's background verbatim-analysis worker.

Re-transcribes saved dictations with a verbatim ASR model (CrisperWhisper by
default) so speech analysis sees the fillers, false starts and repetitions
that the dictation model drops. Everything stays on this machine.
"""

from importlib import metadata

try:
    __version__ = metadata.version("handy-worker")
except metadata.PackageNotFoundError:  # running from a source tree
    __version__ = "0.0.0+source"
