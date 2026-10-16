# Honest Model Evaluation Report (RuleBasedProvider Baseline)

> **EXPLICIT DISCLAIMER & BASELINE DISCLOSURE**:
> The `RuleBasedProvider` evaluated here is an **offline lexicon and pattern baseline**, designed to provide deterministic, zero-network, zero-cost classification when an external LLM provider fails, times out, or trips the circuit breaker.
> A real LLM provider (such as Anthropic Claude or OpenAI GPT-4o) was **NOT** evaluated in this benchmark, as no live API keys or external network calls are permitted in this offline serverless test suite.
> **No tuning was performed on the Holdout set**. The evaluation numbers below are real, uninflated metrics from `scripts/evaluate_providers.py`.

---

## 1. Overall Performance Summary

The evaluation was conducted on two independent sets containing deliberate hard edge cases (sarcasm, negation, typos, code-switching, very short phrases, and long rambling texts):
- **Development Set (`data/dev_dataset.json`)**: 60 synthetic items (Seed: 42).
- **Holdout Set (`data/holdout_dataset.json`)**: 60 synthetic items (Seed: 1337).

| Metric | Development Set | Holdout Set |
|--------|-----------------|-------------|
| **Total Evaluated Samples** | 60 | 60 |
| **Overall Accuracy** | **45.0%** (`27 / 60`) | **40.0%** (`24 / 60`) |
| **Positive F1 Score** | `0.500` | `0.643` |
| **Neutral F1 Score** | `0.462` | `0.453` |
| **Negative F1 Score** | `0.364` | `0.154` |
| **Total Misclassifications** | 33 / 60 | 36 / 60 |

---

## 2. Side-by-Side Per-Class Metrics

### Class Metrics Table

| Sentiment Class | Split | Precision | Recall | F1 Score | Support |
|-----------------|-------|-----------|--------|----------|---------|
| **Positive** | **Dev** | 0.500 | 0.500 | 0.500 | 24 |
| | **Holdout** | 0.562 | 0.750 | 0.643 | 12 |
| **Neutral** | **Dev** | 0.300 | 1.000 | 0.462 | 9 |
| | **Holdout** | 0.324 | 0.750 | 0.453 | 16 |
| **Negative** | **Dev** | 1.000 | 0.222 | 0.364 | 27 |
| | **Holdout** | 0.429 | 0.094 | 0.154 | 32 |

---

## 3. Confusion Matrices

### Development Set Confusion Matrix
```
                 Predicted
              Positive  Neutral  Negative
Actual Positive    12       12        0
Actual Neutral      0        9        0
Actual Negative    12        9        6
```

### Holdout Set Confusion Matrix
```
                 Predicted
              Positive  Neutral  Negative
Actual Positive     9        3        0
Actual Neutral      0       12        4
Actual Negative     7       22        3
```

---

## 4. Top Failure Modes & Error Analysis

The rule-based baseline performs moderately on simple, overt statements, but fails consistently on hard edge cases.

### Failure 1: Sarcasm and Irony
- **Sample Text**: *"Oh wonderful, another update that completely crashes the app. Just fantastic!"*
- **Expected**: `negative` | **Predicted**: `positive`
- **Root Cause**: Lexicon matching counts *"wonderful"* and *"fantastic"* as positive tokens (+2.0), while *"crashes"* only yields (-1.0). The model lacks semantic pragmatics to understand ironic sentiment inversion.

### Failure 2: Code-Switching (Hinglish / Spanglish)
- **Sample Text**: *"Login page pe error aa raha hai, OTP nahi mil raha."*
- **Expected**: `negative` | **Predicted**: `neutral`
- **Root Cause**: While *"error"* was detected, words like *"nahi mil raha"* (Hindi for *not receiving*) are not in the English negation and sentiment lexicon, leading to score neutrality (0.0).

### Failure 3: Negation Flipping Blindness
- **Sample Text**: *"This is definitely not what I would call a good or reliable tool."*
- **Expected**: `negative` | **Predicted**: `neutral`
- **Root Cause**: The negation window flipped *"good"* to negative, but missed *"reliable"*, canceling out the sentiment score back to near zero.

### Failure 4: Long Rambling Complaints
- **Sample Text**: *"I have been a customer since 2021... 504 gateway timeout... billing cycle charges us the full enterprise fee while the platform is unusable..."*
- **Expected**: `negative` | **Predicted**: `neutral`
- **Root Cause**: In rambling paragraphs, neutral corporate vocabulary (*customer*, *enterprise*, *workflow*, *cycle*) dilutes the ratio of sentiment tokens, causing the net score to fall into the `[-0.2, 0.2]` neutral threshold.

---

## 5. Architectural Conclusion

The **40.0% accuracy** on the holdout set demonstrates why a production intelligence pipeline requires:
1. An LLM provider (e.g. Anthropic/Bedrock) as the **primary tier** for high-nuance zero-shot classification and sarcasm comprehension.
2. The `RuleBasedProvider` strictly as a **graceful degradation fallback** to guarantee that customer feedback is never lost or unindexed when third-party AI APIs suffer outages or rate limits.
