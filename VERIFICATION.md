# Documentation pruning verification

PASS within scope, confidence 98%.

- README remains the same concise 24-line landing page. Publisher instructions are absent from current main.
- INSTALL.md retains setup steps and ten byte-identical installation/smoke command blocks. Redundant contributor, architecture, detailed-threshold and advanced-reference discussions are removed. Source/license information has one canonical reference to THIRD_PARTY_NOTICES.md; notices and license files are unchanged.
- Release notes are reduced to six concise lines. Local links, shell syntax and isolated hidden-input key setup/existing-key preservation pass.
- Existing Antigravity tests pass (36 tests, exit 0); no new tests or runtime changes. Earlier runtime evidence is retained, not claimed as a new live run.
- Gemini 4 Argon High authored documentation edits. Jev triage, fresh Gemini reviews and ChatGPT final review pass. This task did not write installed global skills, policy or launchers.

Service availability, quota and authentication state remain external dependencies. Older commits and release assets are retained as historical snapshots.
