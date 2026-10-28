"""Request models for the local Anomizer web interface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RegionModel(BaseModel):
    """One source-time range where blur and/or voice processing is enabled."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    blur: bool = True
    voice: bool = True


class SettingsModel(BaseModel):
    """Everything the browser can change, validated before it reaches FFmpeg."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start: float = Field(default=0.0, ge=0.0)
    # ``None`` is accepted on input and immediately replaced by the source
    # duration in Studio.  The stored snapshot always has a concrete end.
    end: float | None = Field(default=None, gt=0.0)

    blur_enabled: bool = True
    blur_method: Literal["gaussian", "pixelate"] = "gaussian"
    blur_strength: float = Field(default=3.0, ge=1.0, le=5.0)
    blur_margin: float = Field(default=0.22, ge=0.0, le=0.45)
    show_boxes: bool = False

    voice_semitones: float = Field(default=-3.0, ge=-12.0, le=12.0)
    preserve_formants: bool = False
    volume: float = Field(default=1.0, ge=0.0, le=2.0)
    mute: bool = False

    regions: list[RegionModel] = Field(default_factory=list, max_length=200)

    crf: int = Field(default=20, ge=0, le=51)
    preset: Literal[
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    ] = "veryfast"


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    path: str = Field(min_length=1, max_length=8192)
