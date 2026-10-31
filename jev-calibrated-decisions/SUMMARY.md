# Jev — Bundle Contents

- Python CLI/library using only the standard library; `scripts/jev` works via symlink.
- Noul, Choice, and Score for typed judgments; `ask` supports PNG/JPEG assets and Box
  when `--enable-images` is explicitly enabled. Box requires model `jev-1.14.0` or higher.
- Text defaults and the review gate remain pinned to `jev-1.13.0`; do not automatically change the model for
  calibrated thresholds. Images/Box are experimental features and require separately labeled evaluation data.
- `jev-pool` selects slot/stable/round-robin with file locking; it does not automatically fail over keys on API errors.
- API keys belong to machine-local configuration and are excluded from the exported ZIP bundle.
- `scripts/evaluate_results.py` computes offline metrics with ID/hash/model tracking; tests cover
  the evaluator, metadata, key pool, image transport, Box, and launcher.
- `references/images.md` provides examples and official API references; the two images in
  fixtures are synthetic data with and without a red rectangle.

Original client source: https://gist.github.com/AJackTi/6f70237e29767100936e213a22f4340f
(reference revision 6bb90186f27677fbc5a83b57ddcb8ee3aef3fef1).
Current API documentation: https://docs.typesafe.ai/images and
https://docs.typesafe.ai/primitives/box.
