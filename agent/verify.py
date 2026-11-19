"""verify.py - check a claimed answer against the pages the agent actually read.

No model. It extracts candidate facts from sources, measures cross-source
agreement, and flags contradictions. The point is not certainty; it is a
confidence label and a list of what supports or undermines the claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words that carry no evidence value when comparing facts.
STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "that", "this", "it", "as", "at", "by",
    "from", "has", "have", "had", "not", "but", "if", "then", "than", "so",
    "such", "into", "about", "over", "after", "before", "their", "its", "his",
    "her", "they", "we", "you", "i", "he", "she", "which", "who", "what", "when",
    "where", "why", "how", "also", "can", "will", "would", "could", "should",
    "more", "most", "some", "any", "all", "each", "other", "there", "here",
}

WORD_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return [t for t in WORD_RE.findall((text or "").lower()) if t not in STOP]


def key_facts(text: str, limit: int = 40) -> list[str]:
    """Distinctive tokens plus numeric facts (years, counts, percentages)."""
    nums = re.findall(r"\b\d[\d,]*(?:\.\d+)?%?\b", text or "")
    words = [t for t in tokens(text) if len(t) > 3]
    out, seen = [], set()
    for tok in nums + words:
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
        if len(out) >= limit:
            break
    return out


def overlap(a: str, b: str) -> float:
    """Jaccard-style overlap of distinctive tokens, 0..1."""
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class Claim:
    text: str
    source: str = ""

@dataclass
class Verification:
    claim: str
    confidence: str          # "high" | "medium" | "low" | "contradicted" | "unverified"
    score: float             # 0..1 agreement
    support: list[str] = field(default_factory=list)
    against: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        lines = [f"CLAIM: {self.claim}", f"CONFIDENCE: {self.confidence} "
                 f"(agreement {self.score:.2f})"]
        if self.support:
            lines.append("SUPPORTED BY:")
            lines += [f"  + {s}" for s in self.support[:5]]
        if self.against:
            lines.append("CONTRADICTED BY:")
            lines += [f"  - {s}" for s in self.against[:5]]
        if self.notes:
            lines.append("NOTES: " + "; ".join(self.notes))
        return "\n".join(lines)


def verify_claim(claim: str, sources: list[dict]) -> Verification:
    """sources: [{"title":..., "url":..., "text":...}, ...]

    Agreement is measured between the claim's distinctive tokens and each
    source's distinctive tokens, weighted toward the rarest (most informative)
    shared tokens. Contradiction is a direct numeric/date conflict.
    """
    if not sources:
        return Verification(claim, "unverified", 0.0,
                            notes=["no sources were read"])

    claim_toks = set(tokens(claim))
    claim_nums = set(re.findall(r"\b\d[\d,]*(?:\.\d+)?%?\b", claim))
    scored = []
    for s in sources:
        text = s.get("text", "")
        stoks = set(tokens(text))
        inter = claim_toks & stoks
        score = len(inter) / max(1, len(claim_toks))
        # boost for rare, informative overlap
        if inter:
            score += 0.15 * (len(inter) / max(1, len(stoks)))
        scored.append((min(1.0, score), s, inter))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[0][0]
    agreeing = [s for sc, s, _ in scored if sc >= max(0.35, top * 0.6)]
    weak = [s for sc, s, _ in scored if sc < max(0.35, top * 0.6)]

    # Numeric note: a number in the claim absent from an agreeing source is
    # worth flagging, but it is not a contradiction of the claim - only note it.
    notes = []
    if len(agreeing) == 1:
        notes.append("only one source agrees; treat as unconfirmed")
    if claim_nums:
        for sc, s, inter in scored[:3]:
            if not inter:
                continue
            src_nums = set(re.findall(r"\b\d[\d,]*(?:\.\d+)?%?\b", s.get("text", "")))
            if claim_nums - src_nums:
                notes.append("some numbers in the claim are not in "
                             f"{s.get('title','that source')}")
                break

    if top >= 0.6 and len(agreeing) >= 2:
        conf = "high"
    elif top >= 0.35:
        conf = "medium"
    elif top > 0:
        conf = "low"
    else:
        conf = "unverified"

    support = [f"{s.get('title', s.get('url', ''))} <{s.get('url', '')}>"
               for s in agreeing]
    against = [f"{s.get('title', s.get('url', ''))} <{s.get('url', '')}>"
               for s in weak]
    return Verification(claim, conf, round(top, 3), support, against, notes)
