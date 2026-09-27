![Paint Studio](media/paint-studio.png)

<img src="icon.svg" alt="Paint Studio icon" width="96">

# Paint Studio

Custom painting app I use for quick edits, thumbnails and rough concept work. The canvas gets most of the window, with the brush and layer panels kept small on the side.

It has pressure brushes, layers and groups with blend modes and clipping masks, freehand selections, transforms, smart shapes, crash recovery, and its own `.paintstudio` files with PNG export. Scripts and AI agents can also read the canvas and paint into it through a local bridge.

## Running it

It's made for Windows, and I run it on Python 3.12.

```bash
pip install PySide6 numpy
python launch.pyw
python tools/check.py
```

New documents go in `Documents/PaintStudioData`. If you make a `PaintStudioData` folder next to the app it uses that one instead, so it can stay portable.

Closing the window saves the painting as a project and leaves a blank canvas for next time. `Quit Paint Studio` in the title menu does a full exit, and `Open Recent` reopens saved projects with their full undo history.

## Controls

- Click `Paint Studio` in the title bar for the document menu
- `Space` tap to fit and center the canvas, hold and drag to pan
- Top-right navigator recenters on click or drag, zooms with the wheel and fits on double-click
- `Shift+Space` and drag an edge to crop in or reveal painted pixels past the edge (nothing gets erased)
- `End` trims the canvas and deletes anything past the edge
- `M` mirrors the view horizontally, Mirror H and Mirror V in the title bar flip the view without changing the painting
- `C` opens a small color picker under the pointer, releasing on the hue strip keeps it open and releasing in the square picks the color
- `Q` freehand selection, with `Shift` to add, `Alt` to subtract and `Shift+Alt` to intersect
- `Ctrl+T` transforms the selection or selected layers (move, scale, rotate, skew, perspective, pivot), `Enter` applies and `Esc` cancels
- `Ctrl+N` or `Ctrl+W` saves the current painting as a project and starts a new one, a failed save keeps the canvas
- Checkerboard background toggle in the title menu, the default is dark gray

## Layers

- `Insert` new layer
- `'` toggles visibility
- `Ctrl+G` groups the selected layers
- `Home` copies the visible result to a new layer
- `Delete` clears the current layer's pixels (only inside the selection if there is one)
- Drag rows to reorder or move them into groups, right-click the list to create, duplicate, group, rename or delete

## Brushes

- `1` Air, `2` Ink, `3` Paint, `4` Shape, `5` Blur, `6` Stamp, `F` or `7` Fill
- Each brush keeps its own settings between launches
- `E` toggles the eraser for whichever brush is active, `B` goes back to painting
- Fill has opacity and threshold, and can sample the current layer or everything visible

## Smart Shape

On by default, toggle it in the title menu. Draw a rough shape and hold the pointer still, and it flashes and turns into a clean shape you can resize and rotate. `Esc` while still holding goes back to your original stroke.

## Local bridge

While it's running, it takes JSON commands over a local socket. Scripts and AI agents can use it to check the canvas, add layers and paint strokes, and each batch of strokes undoes as one step.

```bash
python tools/paint_api.py '{"command":"canvas_info"}'
```

`canvas_png` writes the canvas to `PaintStudioData/Bridge`, and `canvas_check` tells you if that file still matches the live document. The full command list is in [docs/backend-api.md](docs/backend-api.md).
