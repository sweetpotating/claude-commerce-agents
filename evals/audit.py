"""Audit the rubric judge before trusting its verdicts (and after changing its model):
it must FAIL an empty reply, "I don't know", a confident wrong answer, and an answer to
another question, and PASS the correct one.

    python -m evals.audit
"""

from __future__ import annotations

import asyncio
import sys

from .judge import JUDGE_MODEL, judge

RUBRIC = (
    "PASS if the reply says 30 days from delivery. FAIL if it gives another number of days or "
    "hedges without a number."
)
PROBES = {
    "null": ("", False),
    "dont_know": ("I don't know.", False),
    "wrong": ("You have 14 days to return items.", False),
    "wrong_question": ("Our mugs are 350ml stoneware.", False),
    "oracle": ("You have 30 days from delivery to return an item.", True),
}


def _turn(text: str) -> list[dict]:
    return [
        {
            "kind": "chat",
            "user": "How many days do I have to return an item?",
            "text": text,
            "tools": [],
            "results": [],
            "ui": [],
            "cart": None,
            "error": None,
            "secs": 1,
        }
    ]


async def main() -> int:
    bad = 0
    for name, (text, want) in PROBES.items():
        verdict = await judge(RUBRIC, _turn(text))
        ok = verdict["passed"] is want
        bad += not ok
        print(f"[{'ok' if ok else 'BAD'}] {name}: judged {verdict['passed']} ({verdict['reason'][:100]})")
    print(f"judge {JUDGE_MODEL}: {'trustworthy' if not bad else f'{bad} wrong verdicts'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
