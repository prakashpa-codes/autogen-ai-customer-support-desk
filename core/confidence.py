"""Explainable confidence scoring for a drafted reply.

The score is computed from evidence, never taken from the LLM's opinion of itself:

    confidence = 0.35 * evidence_strength      how relevant the cited passages are
               + 0.25 * citation_coverage      share of factual statements that cite a passage
               + 0.25 * fact_check             share of numbers in the reply found in cited passages
               + 0.15 * category_alignment     cited documents match the ticket category

Every component is in [0, 1] and is stored with the ticket, so a reviewer can see exactly
why a reply scored 0.62 instead of 0.85.
"""

from __future__ import annotations

import re
import unicodedata

from core.retrieval import CATEGORY_SOURCES
from core.schemas import ConfidenceReport, Passage

WEIGHTS = {
    "evidence_strength": 0.35,
    "citation_coverage": 0.25,
    "fact_check": 0.25,
    "category_alignment": 0.15,
}
# Cosine similarity at or above this counts as fully relevant evidence. Calibrated on
# text-embedding-3-small: correct policy passages for the evaluation scenarios scored
# 0.43-0.69 (median about 0.55), unrelated text 0.10-0.36. The relevance gate (MIN_RELEVANCE,
# default 0.40) sits between the two groups.
STRONG_SIMILARITY = 0.55

# [S1] = knowledge-base passage, [O1] = order record
CITATION_RE = re.compile(r"\[([SO]\d+)\]")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+", re.MULTILINE)
COURTESY_RE = re.compile(
    r"\b(sorry|apolog|thank|happy to help|glad to help|feel free|regards|hello|hi |dear|"
    r"i (understand|can see|know)|understand (how|your|that|you)|we're here|let (us|me) know|reach out|"
    r"have a (great|good)|hope (this|that) helps|further assistance|take care)",
    re.IGNORECASE,
)
GREETING_ONLY_RE = re.compile(r"^(hi|hello|dear|namaste|vanakkam)\b[^.!?\n]{0,40}[,!.]?$", re.IGNORECASE)


def strip_citations(text: str) -> str:
    """Customer-facing version of a reply: remove [S1] / [O1] markers."""
    text = re.sub(r"\s*\[(?:[SO]\d+)(?:\s*,\s*[SO]\d+)*\]", "", text)
    text = CITATION_RE.sub("", text)
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", text).strip()


def _ascii_digits(text: str) -> str:
    """Map any Unicode decimal digit (Bengali ২, Tamil ௨, ...) to 0-9 so figures compare across scripts."""
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch for ch in text)


def _numbers(text: str) -> set[str]:
    text = LIST_MARKER_RE.sub("", _ascii_digits(text))  # "1." in a numbered list is not a policy figure
    # "02" (from a date) and "2" are the same figure.
    return {n.replace(",", "").rstrip(".").lstrip("0") or "0" for n in NUMBER_RE.findall(text)}


def _factual_units(reply: str) -> list[str]:
    """Units that state something checkable: paragraphs, with each list item as its own unit.

    The channel template puts a greeting and acknowledgement first and a closing last, so the
    opening and closing paragraphs are skipped unless they state a figure (a bare "Hi Rahul,"
    line and the acknowledgement after it count together as the opening). A citation at the end
    of a paragraph covers that paragraph. Remaining units count when they contain a number or
    8+ words.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", reply.strip()) if p.strip()]
    opening = 2 if paragraphs and GREETING_ONLY_RE.match(paragraphs[0]) else 1
    units: list[tuple[str, bool]] = []  # (text, is_edge_paragraph)
    for index, paragraph in enumerate(paragraphs):
        edge = index < opening or (index == len(paragraphs) - 1 and len(paragraphs) > 1)
        lines = [ln.strip() for ln in paragraph.splitlines() if ln.strip()]
        if any(LIST_MARKER_RE.match(ln) for ln in lines):
            units += [(ln, edge) for ln in lines]
        else:
            units.append((" ".join(lines), edge))

    factual = []
    for text, edge in units:
        plain = CITATION_RE.sub("", text)
        has_number = bool(_numbers(plain))
        if plain.rstrip().endswith(":"):  # a lead-in to a list, not a claim
            continue
        if not has_number and (edge or COURTESY_RE.search(plain)):
            continue
        if has_number or len(plain.split()) >= 8:
            factual.append(text)
    return factual


def assess_confidence(
    reply: str,
    cited: list[Passage],
    retrieved: list[Passage],
    category: str | None,
    customer_text: str,
    min_relevance: float,
    invalid_ids: list[str] | None = None,
) -> ConfidenceReport:
    notes: list[str] = []
    if invalid_ids:
        notes.append(f"Ignored citations to passages that were never retrieved: {', '.join(invalid_ids)}")

    if not reply.strip() or not cited:
        notes.append("The reply cites no knowledge-base passage, so nothing in it can be verified.")
        return ConfidenceReport(score=0.0, components={k: 0.0 for k in WEIGHTS}, notes=notes)

    # 1. Evidence strength: how relevant the best cited passage is, scaled between the relevance
    #    gate (0) and STRONG_SIMILARITY (1). Relevance is the higher of its similarity to the search
    #    query and to the customer's request, so it does not hinge on how the query was worded.
    best = max(max(p.similarity, p.request_similarity) for p in cited)
    span = max(STRONG_SIMILARITY - min_relevance, 1e-6)
    evidence_strength = min(max((best - min_relevance) / span, 0.0), 1.0)
    notes.append(f"Best cited passage relevance {best:.2f} (gate {min_relevance:.2f}, strong {STRONG_SIMILARITY:.2f}).")

    # 2. Citation coverage: factual lines that carry at least one valid citation.
    valid_ids = {p.id for p in cited}
    factual = _factual_units(reply)
    if factual:
        covered = [u for u in factual if set(CITATION_RE.findall(u)) & valid_ids]
        citation_coverage = len(covered) / len(factual)
        notes.append(f"{len(covered)} of {len(factual)} factual statements cite a passage.")
    else:
        citation_coverage = 1.0
        notes.append("No factual statements needed citations.")

    # 3. Fact check: every number in the reply must appear in a cited passage. Numbers the
    #    customer gave us (order IDs, dates), citation labels and list numbering are not policy facts.
    cited_numbers = set().union(*(_numbers(p.text) for p in cited))
    retrieved_numbers = set().union(*(_numbers(p.text) for p in retrieved)) if retrieved else set()
    reply_numbers = _numbers(strip_citations(reply)) - _numbers(customer_text)
    if reply_numbers:
        verified = 0.0
        unverified = []
        for number in sorted(reply_numbers):
            if number in cited_numbers:
                verified += 1
            elif number in retrieved_numbers:
                verified += 0.5  # grounded, but cited from the wrong passage
                unverified.append(f"{number} (in an uncited passage)")
            else:
                unverified.append(number)
        fact_check = verified / len(reply_numbers)
        msg = f"{int(verified) if verified.is_integer() else verified} of {len(reply_numbers)} figures verified in cited passages"
        notes.append(msg + (f"; unverified: {', '.join(unverified)}." if unverified else "."))
    else:
        fact_check = 1.0
        notes.append("The reply states no figures to verify.")

    # 4. Category alignment: did we answer from the documents that own this kind of issue?
    preferred = CATEGORY_SOURCES.get(category or "", set())
    # An order record is the customer's own data, so it belongs to any category.
    if category == "Other" or not preferred or any(p.source in preferred or p.source.startswith("order ") for p in cited):
        category_alignment = 1.0
    else:
        category_alignment = 0.5
        notes.append(f"Cited documents are outside the usual sources for {category} issues.")

    components = {
        "evidence_strength": round(evidence_strength, 3),
        "citation_coverage": round(citation_coverage, 3),
        "fact_check": round(fact_check, 3),
        "category_alignment": round(category_alignment, 3),
    }
    score = sum(WEIGHTS[k] * v for k, v in components.items())
    return ConfidenceReport(score=round(score, 3), components=components, notes=notes)
