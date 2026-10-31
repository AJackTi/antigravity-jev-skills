# Opt-in image and Box requests

The bundled standard-library client supports base64 JSON assets through
`jev ask`, `--assets-file`, `ask -f request.json`, and `JevClient.ask` keyword
arguments. Use the official TypeSafe SDK for multipart uploads. Image/Box
features remain experimental; this is an integration aid, not an accuracy
certificate.

## Construct and send a request

Run from the extracted bundle's parent directory, or change `image_path` to an
authorized PNG/JPEG. This example uses a provided synthetic fixture, not a
private screenshot:

```python
import base64
import json
from pathlib import Path
import uuid

image_path = Path("jev-calibrated-decisions/fixtures/image-positive.png")
reference = str(uuid.uuid4())
request = {
    "model": "jev-1.14.0",
    "state": {"screenshot": reference},
    "assets": {reference: {
        "type": "image", "format": "png",
        "data_base64": base64.b64encode(image_path.read_bytes()).decode("ascii")
    }},
    "questions": {
        "present": {"type": "noul", "instructions": "Is a solid red rectangle visible in `screenshot`?"},
        "target": {"type": "box", "instructions": "the solid red rectangle in `screenshot`"}
    }
}
Path("image-request.json").write_text(json.dumps(request))
```

```bash
jev-pool ask -f image-request.json --enable-images --model jev-1.14.0 --json
```

No image headers are globally enabled. The CLI flag or Python
`enable_images=True` opts in explicitly. `enable_long_context=True` /
`--enable-long-context` independently adds the long-context header. Supplying
headers inside an untrusted request JSON cannot enable either feature.

Python: `client.ask(state, questions, assets=assets, enable_images=True,
model="jev-1.14.0")`. The `box(instructions)` helper builds a Box question.
Box response coordinates/confidence are finite numbers in [0,1], with
left < right and top < bottom, and the returned model must be versioned 1.14+.

## Prepare inputs and interpret results

- Each asset reference must occur in string values of state, instructions or
  criteria; references inside object keys do not count. Use UUIDs to prevent
  accidental substring matches. Images can be examples in Noul/Choice/Score
  criteria as well as in state.
- Supported assets here are PNG/JPEG base64 without a `data:` prefix. The client
  validates the envelope, magic bytes, references, base64 and known byte limits;
  API decoding, dimensions, expanded references and token limits are authoritative.
- Current documented image dimensions are 280–1568 pixels per side, at most
  16 MiB per decoded image and 32 MiB per request. Prepare upright, still images
  before upload; the API strips metadata and does not apply EXIF orientation.
  It rejects out-of-range images instead of resizing them. Use the live docs
  if these experimental limits change.
- A Box always returns a rectangle, even when its target is absent. Read a
  companion presence Noul first. Choose presence/confidence thresholds on your
  own labeled images; none is universally recommended for Box.
- Convert normalized edges to pixels using width for left/right and height for
  top/bottom. Test containment/IoU against labeled boxes in code. The bundled
  positive fixture has expected edges 0.25/0.75; the negative fixture has no
  target, so its Box must be ignored.
- Screenshot review can flag UI overlaps, blank charts and visual evidence for
  a Gemini reviewer. Financial arithmetic and source-data reconciliation still
  need executable checks; an image judgment supplies an observation, not proof.
- Retain the image hash, question version, model, raw typed response and chosen
  action. Request usage includes image input tokens; the older text-only cost
  estimator does not predict image tokens.

## Upstream

- https://docs.typesafe.ai/images
- https://docs.typesafe.ai/primitives/box
- https://typesafe.ai/blog/jev-1-14
- https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md
