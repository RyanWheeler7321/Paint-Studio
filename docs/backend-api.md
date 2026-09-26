# Paint Studio Backend API

`paint_studio/backend.py` is the local bridge over the document engine, so scripts and AI agents can read and paint on the live canvas. A running Paint Studio instance accepts one newline-delimited JSON object per local-socket connection. `tools/paint_api.py` is the normal client.

```bash
python tools/paint_api.py '{"command":"canvas_info"}'
python tools/paint_api.py --file stroke-bundle.json
```

Read commands are `canvas_info`, `canvas_png`, `canvas_check`, and `layers`. `canvas_png` snapshots the document once, writes `PaintStudioData/Bridge/current-canvas.png`, verifies its decoded pixels, then writes `current-canvas.json` last. A short-lived `current-canvas.pending.json` marks an export in progress. If an interruption leaves it behind, `canvas_check` reports `check_valid: false` until a new export completes.

`canvas_check` confirms the PNG and JSON on disk still match the live document. It returns the current document `revision` in the normal response and the saved `check_revision` separately. `check_valid` is only true when the saved revision, dimensions, and document pixel hash match the live document, and the decoded PNG dimensions, pixel hash, byte count, mtime, and SHA-256 match the JSON. A stale file, edited JSON, broken PNG, or interrupted export comes back invalid instead of being accepted.

Editing commands are `set_brush`, `stroke_bundle`, `add_layer`, `add_group`, `set_active_layer`, `set_layer`, `undo`, and `redo`. Layer properties are `name`, `visible`, `opacity`, `blend_mode`, `alpha_locked`, and `clipping`.

A stroke bundle renders once and commits as one undo step. Each stroke can override the active brush, color, and target layer:

```json
{
  "command": "stroke_bundle",
  "bundle": {
    "name": "Block-in",
    "strokes": [
      {
        "layer_id": null,
        "color": "#e3008c",
        "brush": {
          "preset_id": "ink",
          "name": "Ink",
          "size": 36,
          "opacity": 1,
          "flow": 1,
          "spacing": 0.18,
          "hardness": 1,
          "pressure_size": true,
          "pressure_opacity": false,
          "eraser": false
        },
        "samples": [
          {"x": 100, "y": 120, "pressure": 0.3, "timestamp_ms": 0},
          {"x": 420, "y": 180, "pressure": 0.9, "timestamp_ms": 700}
        ]
      }
    ]
  }
}
```

Coordinates are document pixels. Pressure is normalized `0..1`. Omitted brush, color, or layer values use the current document state. The response reports strokes, samples, generated dabs, dirty bounds, render time, and the resulting revision.
