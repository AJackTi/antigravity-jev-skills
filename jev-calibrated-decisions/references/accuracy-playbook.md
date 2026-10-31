# Jev accuracy playbook

Use this reference when a Jev decision will run unattended, affect users, or be described as accurate.

## A. Freeze the decision contract

Record:

- input fields and their provenance;
- question type and exact question text;
- complete criteria, including `other`/`insufficient_evidence`;
- model ID, question version, threshold, and action for abstention;
- what an independent reviewer would mark correct.

Keep the question and threshold in one versioned constant block. Do not change wording, model, labels, or threshold after opening holdout data.

## B. Build the evaluation set

Use JSONL records with stable IDs and a human-reviewed `expected` value. Keep three disjoint files:

```jsonl
{"id":"case-001","expected":true,"state":{"text":"Please refund the duplicate charge."}}
{"id":"case-002","expected":false,"state":{"text":"The invoice is available in the portal."}}
```

Development is for writing questions. Validation is for selecting thresholds and deciding whether to ship. Holdout is opened once after the decision is frozen. Add boundary cases, missing fields, contradictory evidence, prompt-injection-like text, and all languages used in production.

## C. Measure the right thing

Run `scripts/evaluate_results.py` offline. A flat fixture uses `id`, `expected`, and `predicted`; Choice/Score rows also include `confidence`. For production `jev batch` output, keep labels in a separate JSONL file and join by exact stable ID; the evaluator extracts one named question and requires the batch's model and SHA-256 audit metadata.

```bash
python3 scripts/evaluate_results.py --kind noul --input validation.jsonl --threshold 0.80
python3 scripts/evaluate_results.py --kind choice --input validation-choice.jsonl --min-confidence 0.80
python3 scripts/evaluate_results.py --kind score --input validation-score.jsonl --min-confidence 0.80
```

For a structured batch:

```jsonl
{"id":"ticket-001","message":"Please refund the duplicate charge."}
```

Keep the question pointed at `message`, omit `--state-key` so the named field remains in state, and retain the generated `model`, `requested_model`, `question_sha256`, and `state_sha256` fields:

```bash
jev batch --input tickets.jsonl --id-key id --questions-file questions.json --out raw.jsonl --model jev-1.13.0
python3 scripts/evaluate_results.py --kind choice --input raw.jsonl \
  --expected department-labels.jsonl --question department \
  --labels billing technical other --min-confidence 0.80
```

The evaluator rejects provider-error rows, missing answers, duplicate/mismatched IDs, malformed audit metadata, invalid values—even if those rows would otherwise be abstained. For a Noul deployment using an abstention band, evaluate with `--band LOW HIGH`; for Choice, use `--abstain-label insufficient_evidence` when that label is an explicit output.

For `noul`, inspect precision, recall, specificity, Brier score, calibration error, and the forced-versus-accepted confusion matrices. For `choice`, inspect accuracy, coverage, accepted denominator, fixed-label macro F1, and the Wilson interval (an IID descriptive interval, not a model guarantee). For `score`, inspect mean absolute error, within-one-level rate, coverage, and confidence. Do not optimize confidence alone.

A threshold is ready only when it meets the error and coverage requirements written in the contract. If false positives are dangerous, optimize precision; if missed positives are dangerous, optimize recall; if both matter, choose an explicit operating point.

## D. Question design patterns

### Noul

Phrase high as yes. Put the exact boundary in `true` and `false`. For several independent labels, ask one Noul per label and aggregate in code.

### Choice

Use mutually exclusive, same-granularity options. Describe what belongs and what does not belong in each option. Add `other` when a closed list cannot cover the input. Read the runner-up probabilities; a close runner-up is a review signal.

### Score

Describe situations, low to high, rather than numeric labels or vague adjectives. Keep one dimension per scale. A fractional score is a position for ranking/thresholding, not an exact measurement; always inspect the level probabilities.

## E. Robustness checks

Before holdout, run:

1. paraphrases that preserve the label;
2. minimally changed pairs that should flip the label;
3. missing and conflicting evidence;
4. irrelevant distractors added to state;
5. adversarial text inside data fields;
6. exact-match, numeric, date, and schema checks in deterministic code;
7. the production language mix.

When Jev and code disagree, classify the failure as missing evidence, question design, model judgment, or integration bug. Fix the appropriate layer; do not merely raise the threshold.

## F. Shipping policy

Persist raw responses and the evaluation manifest. Log the returned versioned model, not only the alias. Route low-confidence or out-of-distribution cases to review. Re-run validation whenever the model, question, criteria, preprocessing, or action threshold changes. Never claim 'best' without a held-out result for the actual domain.

## Upstream references

Keep the workflow aligned with TypeSafe's current docs:

- https://docs.typesafe.ai/primitives
- https://docs.typesafe.ai/confidence
- https://docs.typesafe.ai/models
- https://docs.typesafe.ai/model-jaggedness/jev-1.13
