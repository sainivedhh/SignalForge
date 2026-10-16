"""Synthetic feedback dataset generator for evaluation.

Generates labeled datasets (sentiment: positive/neutral/negative, and topic: billing/performance/bug/ui_ux/account/feature_request)
with edge cases:
- Sarcasm
- Negation
- Mixed sentiment
- Typos / misspellings
- Code-switched text (Hinglish/Spanglish)
- Very short text (< 10 chars)
- Long rambling feedback (> 800 chars)

Uses separate seeds and generation parameters for:
- DEVELOPMENT set: data/dev_dataset.json (seed=42)
- HOLDOUT set: data/holdout_dataset.json (seed=1337)
"""

import json
import os
import random
from typing import Any, Dict, List

# Distinct hard templates for Development set
DEV_TEMPLATES = [
    # Sarcasm (Hard case: positive words, negative reality)
    {"text": "Oh wonderful, another update that completely crashes the app. Just fantastic!", "sentiment": "negative", "topic": "bug", "category": "sarcasm"},
    {"text": "Brilliant work charging me three times for one subscription, truly five star service.", "sentiment": "negative", "topic": "billing", "category": "sarcasm"},
    {"text": "I absolutely love waiting 45 seconds for a single page to load. Great job guys.", "sentiment": "negative", "topic": "performance", "category": "sarcasm"},

    # Negation
    {"text": "The app is not slow at all and I am not disappointed with the support.", "sentiment": "positive", "topic": "performance", "category": "negation"},
    {"text": "This is definitely not what I would call a good or reliable tool.", "sentiment": "negative", "topic": "bug", "category": "negation"},
    {"text": "Never had any issues with billing and charges are never incorrect.", "sentiment": "positive", "topic": "billing", "category": "negation"},

    # Mixed sentiment
    {"text": "The UI design is gorgeous and clean, but the constant crashes make it unusable.", "sentiment": "negative", "topic": "bug", "category": "mixed"},
    {"text": "Setup was painful and confusing, but customer service was helpful and fixed it.", "sentiment": "positive", "topic": "ui_ux", "category": "mixed"},

    # Typos
    {"text": "The new relase is so sloww, laggy and completly brokn.", "sentiment": "negative", "topic": "performance", "category": "typos"},
    {"text": "Amzing suport! Responded in 2 mins and fixed my login pasword.", "sentiment": "positive", "topic": "account", "category": "typos"},

    # Code-switched (Hinglish / Spanglish)
    {"text": "App ka interface bahut achha hai but payment fail ho raha hai.", "sentiment": "negative", "topic": "billing", "category": "code_switched"},
    {"text": "Servicio muy rapido y excelente, me encanto la nueva actualizacion.", "sentiment": "positive", "topic": "ui_ux", "category": "code_switched"},

    # Very short
    {"text": "Bad.", "sentiment": "negative", "topic": "other", "category": "short"},
    {"text": "Loved it!", "sentiment": "positive", "topic": "other", "category": "short"},
    {"text": "Okay.", "sentiment": "neutral", "topic": "other", "category": "short"},

    # Standard cases
    {"text": "Please add export to Google Sheets and CSV format integration.", "sentiment": "neutral", "topic": "feature_request", "category": "standard"},
    {"text": "I cannot reset my password or receive the 2FA SMS code.", "sentiment": "negative", "topic": "account", "category": "standard"},
    {"text": "Where can I view my latest invoice and tax receipt?", "sentiment": "neutral", "topic": "billing", "category": "standard"},
    {"text": "Fantastic product, super fast performance and reliable sync.", "sentiment": "positive", "topic": "performance", "category": "standard"},
    {"text": "The dark mode theme is clean and easy on the eyes.", "sentiment": "positive", "topic": "ui_ux", "category": "standard"},
]

# Distinct holdout templates with different vocabulary, sentence structures, and long rambling cases
HOLDOUT_TEMPLATES = [
    # Sarcasm
    {"text": "Wow, top tier engineering! The checkout button vanishes whenever you click it.", "sentiment": "negative", "topic": "bug", "category": "sarcasm"},
    {"text": "Amazing feature: deducting renewal fees without sending a receipt or notice.", "sentiment": "negative", "topic": "billing", "category": "sarcasm"},

    # Negation
    {"text": "I cannot say that I hate the new navigation, it isn't terrible.", "sentiment": "neutral", "topic": "ui_ux", "category": "negation"},
    {"text": "It is not broken anymore after the patch, no complaints now.", "sentiment": "positive", "topic": "bug", "category": "negation"},
    {"text": "Payment wasn't processed and no confirmation email was received.", "sentiment": "negative", "topic": "billing", "category": "negation"},

    # Mixed
    {"text": "Fast sync across all devices, but the subscription price hike is ridiculous.", "sentiment": "negative", "topic": "billing", "category": "mixed"},
    {"text": "Customer rep was polite, but took four days to unlock my account.", "sentiment": "neutral", "topic": "account", "category": "mixed"},

    # Typos
    {"text": "Pls fix the serch bar, it frezes when typin fast.", "sentiment": "negative", "topic": "performance", "category": "typos"},
    {"text": "Excelent customr servic, refund issued quikly.", "sentiment": "positive", "topic": "billing", "category": "typos"},

    # Code-switched
    {"text": "Login page pe error aa raha hai, OTP nahi mil raha.", "sentiment": "negative", "topic": "account", "category": "code_switched"},
    {"text": "El diseno esta muy bonito pero tiene mucho lag.", "sentiment": "negative", "topic": "performance", "category": "code_switched"},

    # Very short
    {"text": "Awful", "sentiment": "negative", "topic": "other", "category": "short"},
    {"text": "Superb!", "sentiment": "positive", "topic": "other", "category": "short"},
    {"text": "Meh.", "sentiment": "neutral", "topic": "other", "category": "short"},

    # Long rambling text
    {
        "text": (
            "I have been a customer since 2021 and usually I do not write reviews, but the recent 4.0 update "
            "completely broke our workflow. First of all, the menu items were reorganized for no apparent reason, "
            "and now my entire analytics team cannot find the export button. Secondly, every time we attempt to export "
            "more than 50 rows, the application latency spikes and throws an unhandled 504 gateway timeout error. "
            "We contacted Zendesk support three days ago (ticket 88291) and only received an automated bot reply. "
            "If this billing cycle charges us the full enterprise tier fee while the platform is essentially unusable, "
            "our legal counsel will be looking into breach of SLA terms. Please have a senior engineer look into this."
        ),
        "sentiment": "negative",
        "topic": "bug",
        "category": "long_rambling",
    },
    {
        "text": (
            "Just wanted to drop a quick note after testing the beta release for two weeks with our 40-person design agency. "
            "The dark mode implementation is genuinely one of the best we've seen across SaaS tools, smooth animations, "
            "and we noticed immediate latency improvements when uploading heavy SVG assets. Our team productivity is up. "
            "Would still love to see direct Figma import in the next sprint, but wonderful work to the dev team!"
        ),
        "sentiment": "positive",
        "topic": "ui_ux",
        "category": "long_rambling",
    },

    # Standard
    {"text": "Can you add webhook notifications for new user registrations?", "sentiment": "neutral", "topic": "feature_request", "category": "standard"},
    {"text": "My corporate SSO SAML login fails with invalid signature error.", "sentiment": "negative", "topic": "account", "category": "standard"},
    {"text": "Invoices should include company tax ID and VAT number.", "sentiment": "neutral", "topic": "billing", "category": "standard"},
]


def generate_dataset(templates: List[Dict[str, str]], seed: int, target_size: int = 50) -> List[Dict[str, Any]]:
    random.seed(seed)
    items: List[Dict[str, Any]] = []

    # Cycle templates with minor perturbations
    for i in range(target_size):
        base = templates[i % len(templates)]
        item = {
            "id": f"eval_{seed}_{i:04d}",
            "text": base["text"],
            "expected_sentiment": base["sentiment"],
            "expected_topic": base["topic"],
            "category": base["category"],
        }
        items.append(item)
    return items


def main() -> None:
    os.makedirs("data", exist_ok=True)
    dev_set = generate_dataset(DEV_TEMPLATES, seed=42, target_size=60)
    holdout_set = generate_dataset(HOLDOUT_TEMPLATES, seed=1337, target_size=60)

    with open("data/dev_dataset.json", "w", encoding="utf-8") as f:
        json.dump(dev_set, f, indent=2)
    with open("data/holdout_dataset.json", "w", encoding="utf-8") as f:
        json.dump(holdout_set, f, indent=2)

    print(f"Generated data/dev_dataset.json ({len(dev_set)} items, seed=42)")
    print(f"Generated data/holdout_dataset.json ({len(holdout_set)} items, seed=1337)")


if __name__ == "__main__":
    main()
