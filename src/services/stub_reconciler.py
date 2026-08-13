"""The default, offline cross-file reconciliation seam.

Like every other seam, this stands in for a Groq call so the phase runs — and is
tested — with no network and no API key. Unlike a placeholder, it does the
*actual job*, just narrowly and deterministically: it fixes the single most
common way independently-translated files disagree, which is spelling the same
symbol two different ways.

For each symbol in the contract it derives the plausible alternative spellings
of the agreed name — the same words in snake_case, camelCase, PascalCase,
CONSTANT_CASE, plus the original source-language identifier — and rewrites any
whole-word occurrence of one of those into the name the contract agreed on. So a
file that emitted ``calc_net_pay`` when the table said ``calculateNetPay`` is
brought into line with the file that got it right.

It is deliberately conservative:

* whole-word matches only, so ``calculateNetPayLater`` is never touched;
* the agreed name itself is never rewritten, so the pass is **idempotent** —
  running it twice changes nothing the second time;
* with no contract there is nothing authoritative to align to, so it returns
  the file untouched rather than guessing from the other files' surfaces.

It does rewrite matches inside string literals and comments, which a real Brain
would not; for the offline path that is a fair trade for being deterministic and
dependency-free.
"""

from __future__ import annotations

import re

from models.agent import SwarmAgent
from models.contract import Contract, ContractSymbol
from models.reconcile import ReconcileResult, ReconcileTask

_WORD_SPLIT = re.compile(r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])")


def stub_reconcile(task: ReconcileTask, agent: SwarmAgent) -> ReconcileResult:
    """Rewrite off-contract spellings of shared symbols (no network)."""
    content = task.content
    if task.contract is not None and not task.contract.is_empty:
        content = _apply_contract(content, task.contract)

    return ReconcileResult(
        source_file_id=task.source_file_id,
        target_language=task.target_language,
        content=content,
        agent_id=agent.id,
        tokens_used=len(task.content) if content != task.content else 0,
        duration_ms=1,
    )


def _apply_contract(content: str, contract: Contract) -> str:
    for symbol in contract.symbols:
        for alias in _aliases(symbol):
            content = re.sub(
                rf"\b{re.escape(alias)}\b", symbol.target_name, content
            )
    return content


def _aliases(symbol: ContractSymbol) -> list[str]:
    """Spellings of ``symbol`` that should become its agreed target name."""
    words = [w.lower() for w in _WORD_SPLIT.split(symbol.target_name) if w]
    if not words:  # pragma: no cover - contract names are never empty
        return []
    candidates = {
        "_".join(words),                                    # snake_case
        "".join(w.capitalize() for w in words),             # PascalCase
        words[0] + "".join(w.capitalize() for w in words[1:]),  # camelCase
        "_".join(words).upper(),                            # CONSTANT_CASE
        symbol.source_name,
    }
    # Never rewrite the agreed name into itself: that is what makes this
    # idempotent, and what stops a no-op pass from being reported as a change.
    candidates.discard(symbol.target_name)
    return sorted(candidates)
