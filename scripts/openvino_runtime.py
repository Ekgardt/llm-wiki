"""Compile a pinned model with OpenVINO on the CPU, with Intel's usage telemetry declined first.

`import openvino` imports its model converter, and the converter sends a usage
event to Intel unless the consent file says no; a missing file counts as consent
(`openvino_telemetry.utils.opt_in_checker.check`). So the file is written with
`0`, the value that declines, before the first import, and a choice already on
disk is left as it is. A consent that cannot be written stops the load: the
memory sends nothing anywhere (owner message 17943).

The default precision is the engine's own choice for the CPU (bf16 where the
CPU has it). On the reranker it was 1.8x faster than f32, with MRR@10 equal on
the English and cross-language sets and 0.911 against 0.920 on the Russian one;
`LLMWIKI_INFERENCE_PRECISION=f32` is the operator's way back.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

TELEMETRY_DECLINED = "0"
PRECISION_ENV = "LLMWIKI_INFERENCE_PRECISION"
F32 = "f32"
# The thread count a stand hands each worker (`benchmark/run_longmemeval.worker_threads`);
# OpenVINO does not read it on its own.
THREADS_ENV = "OMP_NUM_THREADS"


def telemetry_consent_file() -> Path:
    """Where `openvino_telemetry` reads the consent from on this platform."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / "Intel Corporation" / "openvino_telemetry"
    return Path.home() / "intel" / "openvino_telemetry"


def telemetry_declined() -> bool:
    """Whether the consent file holds the value that sends nothing."""
    try:
        recorded = telemetry_consent_file().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == TELEMETRY_DECLINED


def decline_telemetry() -> None:
    """Write the declining consent when none is recorded yet; keep one that is."""
    consent = telemetry_consent_file()
    if consent.exists():
        return
    consent.parent.mkdir(parents=True, exist_ok=True)
    consent.write_text(TELEMETRY_DECLINED + "\n", encoding="utf-8")


def compile_config() -> dict[str, Any]:
    """The CPU plugin's settings this process asks for."""
    config: dict[str, Any] = {}
    threads = os.environ.get(THREADS_ENV, "").strip()
    if threads.isdigit() and int(threads) > 0:
        config["INFERENCE_NUM_THREADS"] = int(threads)
    if os.environ.get(PRECISION_ENV, "").strip().lower() == F32:
        config["INFERENCE_PRECISION_HINT"] = F32
    return config


def compiled_model(path: Path) -> Any:
    """The model at `path`, compiled for the CPU."""
    decline_telemetry()
    import openvino

    return openvino.Core().compile_model(str(path), "CPU", compile_config())


def precision_name() -> str:
    """What the operator asked for: the engine's default unless told f32."""
    return compile_config().get("INFERENCE_PRECISION_HINT", "auto")
