# AdaptiveNet cause classifier: how to train it

The model reads a table of numbers per region per tick and predicts the cause:
healthy, network, compute or critical. It uses scikit-learn, not YOLO.
YOLO detects objects in images, and this data has no images.

## 1. Install (once)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install scikit-learn pandas joblib
```

## 2. Practise with fake data (5 minutes)

```bash
python make_practice_ticks.py --out data/ticks.jsonl
python train_classifier.py --data data/ticks.jsonl
```

The fake data is simulated and too clean, so expect scores near 1.0.
Never report these numbers. This step only checks that the pipeline runs.

## 3. Collect real lab data

Run the Docker lab, inject faults through the experiment runner, call
`POST /controller/tick` with auto-mitigation off, and write one JSON line
per region per tick with the same fields as the practice file:

```
run_id, split, tick_index, region,
int_p95_ms, int_error_ratio, int_samples,
ext_p95_ms, ext_error_ratio, ext_samples, label
```

`split` is `train` for the usual fault sizes and `heldout` for the
sizes you keep back for testing. `label` comes from the fault you injected,
never from the controller's own decision.

## 4. Train and evaluate on real data

```bash
python train_classifier.py --data data/ticks.jsonl
```

It prints, in this order:
- macro-F1 from 5-fold cross-validation, split by run so no run leaks
- the Table I rules and the tree on held-out fault sizes: macro-F1,
  false action rate, confusion matrix
- the learned tree as text, which you can put in the report
- the saved model at `models/cause-tree-v1.joblib`

## 5. Use the model

Load it in `ml/predictor.py`:

```python
bundle = joblib.load("models/cause-tree-v1.joblib")
cause = bundle["model"].predict(features_df[bundle["features"]])
```

Then map the cause to an action. See the plan doc, step 5.
