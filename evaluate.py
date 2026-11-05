"""Evaluate ResolveDesk end to end against labelled scenarios (real model, real retrieval).

    python evaluate.py                       # all scenarios, round-robin team
    python evaluate.py --mode selector       # same scenarios with SelectorGroupChat
    python evaluate.py --only PDF-5 OWN-2    # a subset
    python evaluate.py --retrieval-only      # retrieval metrics only (embeddings, no chat model)
    python evaluate.py --no-judge            # skip the LLM-judge faithfulness check

Each reply is also scored by an LLM judge: what share of its claims the evidence passages support.
Writes evaluation/results_<mode>.csv, evaluation/report_<mode>.md and evaluation/retrieval_results.csv.
Runs use a separate store in evaluation/run/, so the live ticket log is not touched.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import shutil
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from autogen_core.models import SystemMessage, UserMessage
from autogen_ext.models.openai import OpenAIChatCompletionClient
from pydantic import BaseModel, Field

from config import ROOT, settings
from core.orders import OrderBook
from core.retrieval import KnowledgeBase, OpenAIEmbedder
from core.schemas import TicketContext
from core.store import TicketStore
from team import create_model_client, run_ticket

EVAL_DIR = ROOT / "evaluation"
SCENARIOS = json.loads((EVAL_DIR / "scenarios.json").read_text(encoding="utf-8"))

JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4o")  # a stronger model than the one that wrote the reply

JUDGE_PROMPT = """You check a customer support reply for faithfulness to the evidence it was written from.
Extract each factual claim the reply makes about the company or the customer's order: policies, steps,
amounts, time limits, order details, product facts. For each one, decide whether the EVIDENCE states it
or it follows directly from the evidence (same meaning in other words counts as supported), and give the
id of the supporting passage.
Do NOT list as claims: greetings, apologies, empathy, offers of further help, questions asking the customer
for details, restating what the customer said, or honest statements that information is not available.
The reply may be in any language; judge its meaning."""


class Claim(BaseModel):
    claim: str = Field(description="The claim, quoted briefly")
    supported: bool
    passage_id: str = Field(description="Supporting passage id such as S1 or O1, or empty if unsupported")


class Faithfulness(BaseModel):
    claims: list[Claim]


async def judge_faithfulness(ctx: TicketContext) -> tuple[float | str, str]:
    """LLM judge: share of the reply's factual claims supported by the evidence ("n/a" for honest no-match)."""
    if ctx.no_match or not ctx.evidence:
        return "n/a", ""  # nothing to be faithful to; the honest-reply check is source_ok
    evidence = "\n\n".join(f"[{p.id}] {p.source} > {p.section}\n{p.text}" for p in ctx.evidence)
    client = OpenAIChatCompletionClient(model=JUDGE_MODEL, api_key=settings.openai_api_key, temperature=0)
    try:
        result = await client.create(
            [SystemMessage(content=JUDGE_PROMPT),
             UserMessage(content=f"CUSTOMER MESSAGE:\n{ctx.complaint}\n\nEVIDENCE:\n{evidence}\n\n"
                                 f"REPLY:\n{ctx.reply or ctx.customer_reply}", source="evaluator")],
            json_output=Faithfulness,
        )
    finally:
        await client.close()
    claims = Faithfulness.model_validate_json(result.content).claims
    score = sum(c.supported for c in claims) / len(claims) if claims else 1.0
    return round(score, 2), "; ".join(c.claim for c in claims if not c.supported)


def retrieval_eval(kb: KnowledgeBase, scenarios: list[dict], k: int) -> tuple[list[dict], dict]:
    """Hit-rate@k and MRR: does an expected source appear among the top-k chunks for the complaint?"""
    rows = []
    for s in scenarios:
        expected = set(s["expected"].get("sources", []))
        if not expected:
            continue
        results = kb.search(s["complaint"], s["expected"]["category"][0], k)
        rank = next((i + 1 for i, r in enumerate(results) if r.chunk.source in expected), None)
        rows.append({
            "id": s["id"],
            "hit": int(rank is not None),
            "rank": rank or "",
            "reciprocal_rank": round(1 / rank, 3) if rank else 0.0,
            "top_source": results[0].chunk.source if results else "",
            "top_similarity": round(results[0].similarity, 3) if results else 0.0,
        })
    summary = {
        f"hit_rate@{k}": round(statistics.mean(r["hit"] for r in rows), 3),
        "mrr": round(statistics.mean(r["reciprocal_rank"] for r in rows), 3),
        "queries": len(rows),
    }
    return rows, summary


async def run_scenario(s: dict, kb: KnowledgeBase, store: TicketStore, mode: str, judge: bool) -> dict[str, Any]:
    created = store.create_ticket(
        s["complaint"], channel=s["channel"], customer_name=s.get("customer_name", ""), order_id=s.get("order_id", ""),
        subject=s.get("subject", ""),
    )
    ctx = TicketContext(
        ticket_id=created["ticket_id"], complaint=s["complaint"], customer_name=s.get("customer_name", ""),
        order_id=s.get("order_id", ""), channel=s["channel"], subject=s.get("subject", ""),
        # Scenarios are raised the way staff raise tickets from the dashboard.
        identity_verified=True, raised_by_staff=True,
    )
    speakers: list[str] = []

    async def emit(event_type: str, agent: str | None, data: dict) -> None:
        if event_type == "agent_completed" and agent:
            speakers.append(agent)

    client = create_model_client(settings)
    try:
        meta = await run_ticket(ctx, kb, settings, store.record_processed_ticket, emit, client, mode=mode,
                                orders=OrderBook.load(settings.orders_path))
    finally:
        await client.close()

    exp = s["expected"]
    t = ctx.triage
    sources = sorted({p.source for p in ctx.cited_passages} or {p.source for p in ctx.evidence})
    row = {
        "id": s["id"],
        "group": s["group"],
        "complaint": s["complaint"],
        "category": t.category if t else "",
        "urgency": t.urgency if t else "",
        "sentiment": t.sentiment if t else "",
        "language": t.language if t else "",
        "status": ctx.decision.status if ctx.decision else "",
        "reasons": "; ".join(ctx.decision.reasons) if ctx.decision else "",
        "kb_sources": "; ".join(sources) or "none",
        "confidence": ctx.confidence.score if ctx.confidence else 0.0,
        "agent_order": " > ".join(speakers),
        "seconds": round(meta["duration_ms"] / 1000, 1),
        "tokens": meta["prompt_tokens"] + meta["completion_tokens"],
        "category_ok": int(t is not None and t.category in exp["category"]),
        "status_ok": int(ctx.decision is not None and ctx.decision.status == exp["status"]),
        "source_ok": int(ctx.no_match if exp.get("no_match") else bool(set(sources) & set(exp.get("sources", [])))),
        "urgency_ok": "" if "urgency" not in exp else int(t is not None and t.urgency == exp["urgency"]),
        "sentiment_ok": "" if "sentiment" not in exp else int(t is not None and t.sentiment == exp["sentiment"]),
        "language_ok": "" if "language" not in exp else int(t is not None and exp["language"].lower() in t.language.lower()),
        "faithfulness": "",
        "unsupported_claims": "",
        "reply": ctx.customer_reply,
    }
    if judge:
        row["faithfulness"], row["unsupported_claims"] = await judge_faithfulness(ctx)
    mark = "PASS" if row["category_ok"] and row["status_ok"] and row["source_ok"] else "CHECK"
    faith = f"  faith {row['faithfulness']}" if judge else ""
    print(f"[{mark}] {s['id']:<7} {row['category']:<9} {row['urgency']:<6} {row['sentiment']:<10} "
          f"{row['status']:<9} conf {row['confidence']:.2f}{faith}  {row['kb_sources']}")
    return row


def _rate(rows: list[dict], key: str) -> str:
    values = [r[key] for r in rows if r[key] != ""]
    return f"{sum(values)}/{len(values)}" if values else "-"


def write_report(rows: list[dict], retrieval: dict, mode: str) -> None:
    escalate_expected = {s["id"] for s in SCENARIOS if s["expected"]["status"] == "Escalated"}
    tp = sum(1 for r in rows if r["status"] == "Escalated" and r["id"] in escalate_expected)
    fp = sum(1 for r in rows if r["status"] == "Escalated" and r["id"] not in escalate_expected)
    fn = sum(1 for r in rows if r["status"] != "Escalated" and r["id"] in escalate_expected)
    judged = [r["faithfulness"] for r in rows if isinstance(r["faithfulness"], float)]
    faithfulness = (f"{statistics.mean(judged):.2f} average · {sum(f == 1.0 for f in judged)}/{len(judged)} replies fully supported "
                    f"(judge `{JUDGE_MODEL}`; honest no-match replies not scored)"
                    if judged else "not run")
    lines = [
        "# Evaluation report",
        "",
        f"Run: {datetime.now():%Y-%m-%d %H:%M} · model `{settings.model}` · embeddings `{settings.embedding_model}` · "
        f"team mode `{mode}` · confidence threshold {settings.confidence_threshold} · relevance gate {settings.min_relevance}",
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Scenarios | {len(rows)} |",
        f"| Category correct | {_rate(rows, 'category_ok')} |",
        f"| Resolved / Escalated decision correct | {_rate(rows, 'status_ok')} |",
        f"| Expected knowledge source used (or honest no-match) | {_rate(rows, 'source_ok')} |",
        f"| Urgency correct (where labelled) | {_rate(rows, 'urgency_ok')} |",
        f"| Sentiment correct (where labelled) | {_rate(rows, 'sentiment_ok')} |",
        f"| Language detected (where labelled) | {_rate(rows, 'language_ok')} |",
        f"| Escalation precision / recall | {tp}/{tp + fp} · {tp}/{tp + fn} |",
        f"| Average confidence | {statistics.mean(r['confidence'] for r in rows):.2f} |",
        f"| Faithfulness (LLM judge) | {faithfulness} |",
        f"| Average time per ticket | {statistics.mean(r['seconds'] for r in rows):.1f} s |",
        f"| Average tokens per ticket | {statistics.mean(r['tokens'] for r in rows):,.0f} |",
        f"| Retrieval hit-rate@{settings.top_k} / MRR | {retrieval.get(f'hit_rate@{settings.top_k}')} / {retrieval.get('mrr')} |",
        "",
        "## Per scenario",
        "",
        "| ID | Category | Urgency | Sentiment | Status | Confidence | Faithfulness | Sources | Checks |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        checks = "✓" if r["category_ok"] and r["status_ok"] and r["source_ok"] else "✗"
        faith = r["faithfulness"] if r["faithfulness"] != "" else "-"
        lines.append(f"| {r['id']} | {r['category']} | {r['urgency']} | {r['sentiment']} | {r['status']} | "
                     f"{r['confidence']:.2f} | {faith} | {r['kb_sources']} | {checks} |")
    unsupported = [r for r in rows if r["unsupported_claims"]]
    if unsupported:
        lines += ["", "## Claims the judge found unsupported", ""]
        lines += [f"- **{r['id']}**: {r['unsupported_claims']}" for r in unsupported]
    (EVAL_DIR / f"report_{mode}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["round_robin", "selector"], default=settings.team_mode)
    parser.add_argument("--only", nargs="*", help="Scenario IDs to run")
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM-judge faithfulness check")
    args = parser.parse_args()

    scenarios = [s for s in SCENARIOS if not args.only or s["id"] in args.only]
    kb = KnowledgeBase(settings.kb_dir, OpenAIEmbedder(settings.embedding_model, settings.openai_api_key),
                       settings.index_dir).build()

    retrieval_rows, retrieval = retrieval_eval(kb, scenarios, settings.top_k)
    write_csv(EVAL_DIR / "retrieval_results.csv", retrieval_rows)
    print(f"Retrieval: {retrieval}")
    if args.retrieval_only:
        return

    run_dir = EVAL_DIR / "run"
    shutil.rmtree(run_dir, ignore_errors=True)
    store = TicketStore(run_dir / "eval.db", run_dir / "logs")
    rows = []
    for s in scenarios:
        rows.append(await run_scenario(s, kb, store, args.mode, judge=not args.no_judge))
    store.close()

    write_csv(EVAL_DIR / f"results_{args.mode}.csv", rows)
    write_report(rows, retrieval, args.mode)
    print(f"Wrote evaluation/results_{args.mode}.csv and evaluation/report_{args.mode}.md ({len(rows)} scenarios).")


if __name__ == "__main__":
    asyncio.run(main())
