"""Render errors and restricted encoder fallback."""
from __future__ import annotations
from typing import Callable, TypeVar


class RenderError(RuntimeError): pass


class EncoderError(RenderError):
    def __init__(self, message: str, *, stderr: str = "", returncode: int | None = None):
        super().__init__(message); self.stderr, self.returncode = stderr, returncode


FALLBACK_MARKERS = ("unknown encoder", "no capable devices found", "cannot load nvcuda",
                    "nvenc initialization failed", "no nvenc capable devices found")


def is_encoder_fallback_error(error: BaseException | str) -> bool:
    text = error if isinstance(error, str) else f"{error} {getattr(error, 'stderr', '')}"
    return any(marker in text.casefold() for marker in FALLBACK_MARKERS)


def should_fallback_to_cpu(error: BaseException | str) -> bool:
    return is_encoder_fallback_error(error)


T = TypeVar("T")


def run_with_encoder_fallback(render: Callable[[str], T], encoder: str, *,
                              fallback_encoder: str = "libx264") -> tuple[T, str]:
    try: return render(encoder), encoder
    except Exception as error:
        if encoder == fallback_encoder or not is_encoder_fallback_error(error): raise
        return render(fallback_encoder), fallback_encoder


__all__ = ["EncoderError", "FALLBACK_MARKERS", "RenderError", "is_encoder_fallback_error",
           "run_with_encoder_fallback", "should_fallback_to_cpu"]
