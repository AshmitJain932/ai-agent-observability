# Documentation assets

| File | What it is |
|---|---|
| `safety-demo.gif` | A terminal session of [`examples/support_agent/protect_example.py`](../../examples/support_agent/protect_example.py): the agent is asked for the same refund three times (twice in one process, then after a restart) and the guarded refund tool runs once. 800 px wide, about 9 s, loops. |
| `safety-demo.svg` | The same session as an animated SVG (SMIL, no script), for places that prefer vector images. |
| `make_safety_demo.py` | Generates both. |

The text in the animation is the example's real standard output, captured when the files are
generated; only colours are added. The generator also runs the example once more with a counter
around the refund tool and refuses to write anything unless the output is identical and the tool
ran exactly once, which is what the caption says.

## Regenerating

After a change to the example or its output:

```bash
uv run --with pillow python docs/assets/make_safety_demo.py
uv run --with pillow python docs/assets/make_safety_demo.py --frames /tmp/frames  # also save each frame as a PNG
```

Pillow is not a project dependency: `--with pillow` adds it for that one run. Nothing else is
needed beyond a monospace TrueType font: DejaVu Sans Mono (Debian/Ubuntu package
`fonts-dejavu-core`) or Liberation Mono is found in the usual places, or pass `--font PATH`. The
committed files were made with DejaVu Sans Mono and Pillow 13.0. With the same font file and
Pillow version the output is byte-for-byte the same, so a diff in `safety-demo.gif` means the
example's output, the font or the script changed.
