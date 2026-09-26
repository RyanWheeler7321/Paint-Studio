from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True, slots=True)
class BrushSettings:
    preset_id: str = "air"
    name: str = "Air"
    engine: str = "air"
    size: float = 24.0
    opacity: float = 1.0
    flow: float = 1.0
    spacing: float = 0.18
    hardness: float = 1.0
    pressure_size: bool = True
    pressure_opacity: bool = False
    eraser: bool = False
    tip_shape: str = "circle"
    fill_mode: str = "fill"
    primitive: str = "rect"
    border_width: int = 6
    corner_radius: int = 0
    texture_strength: float = 0.65
    tilt_stretch: bool = True
    blur_radius: int = 10
    blur_strength: float = 0.7
    scatter: float = 0.0
    size_variation: float = 0.0
    rotation_variation: float = 0.0
    hue_variation: float = 0.0
    alpha_variation: float = 0.0
    size_x_variation: float = 0.0
    size_y_variation: float = 0.0
    value_variation: float = 0.0
    saturation_variation: float = 0.0
    flip_x: bool = False
    flip_y: bool = False
    follow_rotation: bool = True
    fill_threshold: int = 24
    fill_reference: str = "current"

    def normalized(self) -> "BrushSettings":
        return replace(
            self,
            size=max(1.0, min(float(self.size), 5000.0)),
            opacity=max(0.0, min(float(self.opacity), 1.0)),
            flow=max(0.01, min(float(self.flow), 1.0)),
            spacing=max(0.02, min(float(self.spacing), 2.0)),
            hardness=max(0.0, min(float(self.hardness), 1.0)),
            border_width=max(1, min(int(self.border_width), 240)),
            corner_radius=max(0, min(int(self.corner_radius), 128)),
            texture_strength=max(0.0, min(float(self.texture_strength), 1.0)),
            blur_radius=max(1, min(int(self.blur_radius), 120)),
            blur_strength=max(0.01, min(float(self.blur_strength), 1.0)),
            scatter=max(0.0, min(float(self.scatter), 1.0)),
            size_variation=max(0.0, min(float(self.size_variation), 1.0)),
            rotation_variation=max(0.0, min(float(self.rotation_variation), 180.0)),
            hue_variation=max(0.0, min(float(self.hue_variation), 180.0)),
            alpha_variation=max(0.0, min(float(self.alpha_variation), 1.0)),
            size_x_variation=max(0.0, min(float(self.size_x_variation), 1.0)),
            size_y_variation=max(0.0, min(float(self.size_y_variation), 1.0)),
            value_variation=max(0.0, min(float(self.value_variation), 1.0)),
            saturation_variation=max(0.0, min(float(self.saturation_variation), 1.0)),
            fill_threshold=max(0, min(int(self.fill_threshold), 255)),
            fill_reference="visible" if self.fill_reference == "visible" else "current",
        )

    def changed(self, **values: object) -> "BrushSettings":
        return replace(self, **values).normalized()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "BrushSettings":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields}).normalized()


BRUSH_PRESETS: tuple[BrushSettings, ...] = (
    BrushSettings(hardness=0.0, pressure_size=False, pressure_opacity=True),
    BrushSettings(
        preset_id="ink",
        name="Ink",
        engine="ink",
        hardness=1.0,
        pressure_size=True,
    ),
    BrushSettings(
        preset_id="paint",
        name="Paint",
        engine="paint",
        tip_shape="texture",
        hardness=0.58,
        flow=0.88,
        pressure_size=False,
        pressure_opacity=True,
    ),
    BrushSettings(
        preset_id="shape",
        name="Shape",
        engine="shape",
        hardness=1.0,
        pressure_size=True,
        pressure_opacity=True,
    ),
    BrushSettings(
        preset_id="blur",
        name="Blur",
        engine="blur",
        spacing=0.06,
        pressure_size=True,
        pressure_opacity=True,
    ),
    BrushSettings(
        preset_id="stamp",
        name="Stamp",
        engine="stamp",
        tip_shape="custom",
        pressure_size=False,
        pressure_opacity=True,
    ),
)


BRUSH_PRESETS += (BrushSettings(preset_id="fill", name="Fill", engine="fill", pressure_size=False),)


def brush_preset(preset_id: str) -> BrushSettings:
    return next((preset for preset in BRUSH_PRESETS if preset.preset_id == preset_id), BRUSH_PRESETS[0])
