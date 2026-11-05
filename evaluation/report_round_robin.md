# Evaluation report

Run: 2026-11-05 13:39 · model `gpt-4o-mini` · embeddings `text-embedding-3-small` · team mode `round_robin` · confidence threshold 0.7 · relevance gate 0.4

## Summary

| Metric | Result |
|---|---|
| Scenarios | 20 |
| Category correct | 20/20 |
| Resolved / Escalated decision correct | 20/20 |
| Expected knowledge source used (or honest no-match) | 20/20 |
| Urgency correct (where labelled) | 2/2 |
| Sentiment correct (where labelled) | 1/1 |
| Language detected (where labelled) | 3/3 |
| Escalation precision / recall | 5/5 · 5/5 |
| Average confidence | 0.86 |
| Faithfulness (LLM judge) | 1.00 average · 18/18 replies fully supported (judge `gpt-4o`; honest no-match replies not scored) |
| Average time per ticket | 10.4 s |
| Average tokens per ticket | 8,524 |
| Retrieval hit-rate@5 / MRR | 1 / 0.972 |

## Per scenario

| ID | Category | Urgency | Sentiment | Status | Confidence | Faithfulness | Sources | Checks |
|---|---|---|---|---|---|---|---|---|
| PDF-1 | Shipping | Medium | Frustrated | Resolved | 0.87 | 1.0 | shipping_policy.md | ✓ |
| PDF-2 | Refund | Medium | Neutral | Resolved | 0.88 | 1.0 | refund_policy.md | ✓ |
| PDF-3 | Account | Medium | Neutral | Resolved | 1.00 | 1.0 | account_and_login.md | ✓ |
| PDF-4 | Product | Medium | Frustrated | Resolved | 1.00 | 1.0 | order ORD-48877; warranty_and_replacements.md | ✓ |
| PDF-5 | Billing | High | Angry | Escalated | 1.00 | 1.0 | billing_and_payments.md; order ORD-49012 | ✓ |
| PDF-6 | Other | Low | Neutral | Escalated | 0.00 | n/a | none | ✓ |
| OWN-1 | Product | Medium | Frustrated | Resolved | 1.00 | 1.0 | warranty_and_replacements.md | ✓ |
| OWN-2 | Product | Medium | Neutral | Resolved | 1.00 | 1.0 | order ORD-50410; refund_policy.md | ✓ |
| OWN-3 | Shipping | Medium | Frustrated | Resolved | 0.94 | 1.0 | order ORD-50577; shipping_policy.md | ✓ |
| OWN-4 | Refund | Medium | Neutral | Resolved | 0.92 | 1.0 | order ORD-50689; refund_policy.md | ✓ |
| OWN-5 | Account | High | Frustrated | Escalated | 0.94 | 1.0 | account_and_login.md; billing_and_payments.md | ✓ |
| OWN-6 | Product | High | Frustrated | Escalated | 1.00 | 1.0 | warranty_and_replacements.md | ✓ |
| OWN-7 | Other | Low | Neutral | Resolved | 0.86 | 1.0 | team_and_academy_orders.md | ✓ |
| OWN-8 | Billing | Low | Neutral | Resolved | 1.00 | 1.0 | billing_and_payments.md | ✓ |
| OWN-9 | Product | Low | Neutral | Resolved | 1.00 | 1.0 | bat_care_and_services.md | ✓ |
| OWN-10 | Account | Medium | Frustrated | Resolved | 1.00 | 1.0 | account_and_login.md; resolved_cases.md | ✓ |
| OWN-11 | Product | Low | Neutral | Resolved | 1.00 | 1.0 | product_faq.md | ✓ |
| OWN-12 | Refund | Medium | Neutral | Resolved | 0.88 | 1.0 | orders_and_cancellations.md; refund_policy.md | ✓ |
| OWN-13 | Account | Low | Neutral | Resolved | 1.00 | 1.0 | account_and_login.md | ✓ |
| OWN-14 | Other | Low | Neutral | Escalated | 0.00 | n/a | none | ✓ |
