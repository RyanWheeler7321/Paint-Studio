from __future__ import annotations

from dataclasses import dataclass, field
import uuid

from PySide6.QtGui import QImage, QPainter


BLEND_MODES: tuple[str, ...] = (
    "Normal",
    "Multiply",
    "Screen",
    "Overlay",
    "Darken",
    "Lighten",
    "Color Dodge",
    "Color Burn",
    "Hard Light",
    "Soft Light",
    "Difference",
    "Exclusion",
    "Addition",
)


BLEND_COMPOSITION: dict[str, QPainter.CompositionMode] = {
    "Normal": QPainter.CompositionMode.CompositionMode_SourceOver,
    "Multiply": QPainter.CompositionMode.CompositionMode_Multiply,
    "Screen": QPainter.CompositionMode.CompositionMode_Screen,
    "Overlay": QPainter.CompositionMode.CompositionMode_Overlay,
    "Darken": QPainter.CompositionMode.CompositionMode_Darken,
    "Lighten": QPainter.CompositionMode.CompositionMode_Lighten,
    "Color Dodge": QPainter.CompositionMode.CompositionMode_ColorDodge,
    "Color Burn": QPainter.CompositionMode.CompositionMode_ColorBurn,
    "Hard Light": QPainter.CompositionMode.CompositionMode_HardLight,
    "Soft Light": QPainter.CompositionMode.CompositionMode_SoftLight,
    "Difference": QPainter.CompositionMode.CompositionMode_Difference,
    "Exclusion": QPainter.CompositionMode.CompositionMode_Exclusion,
    "Addition": QPainter.CompositionMode.CompositionMode_Plus,
}


def new_layer_id() -> str:
    return uuid.uuid4().hex


@dataclass(slots=True)
class LayerNode:
    name: str
    kind: str = "paint"
    layer_id: str = field(default_factory=new_layer_id)
    visible: bool = True
    opacity: float = 1.0
    blend_mode: str = "Normal"
    alpha_locked: bool = False
    clipping: bool = False
    image: QImage | None = None
    children: list["LayerNode"] = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    def clone(self) -> "LayerNode":
        return LayerNode(
            name=self.name,
            kind=self.kind,
            layer_id=self.layer_id,
            visible=self.visible,
            opacity=self.opacity,
            blend_mode=self.blend_mode,
            alpha_locked=self.alpha_locked,
            clipping=self.clipping,
            image=QImage(self.image) if self.image is not None else None,
            children=[child.clone() for child in self.children],
        )


def transparent_image(width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    return image
