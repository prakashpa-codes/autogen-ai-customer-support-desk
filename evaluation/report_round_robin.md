# Evaluation report

Run: 2026-11-03 21:40 · model `gpt-4o-mini` · embeddings `text-embedding-3-small` · team mode `round_robin` · confidence threshold 0.7 · relevance gate 0.4

## Summary

| Metric | Result |
|---|---|
| Scenarios | 16 |
| Category correct | 16/16 |
| Resolved / Escalated decision correct | 16/16 |
| Expected knowledge source used (or honest no-match) | 16/16 |
| Urgency correct (where labelled) | 2/2 |
| Sentiment correct (where labelled) | 1/1 |
| Language detected (where labelled) | 2/2 |
| Escalation precision / recall | 4/4 · 4/4 |
| Average confidence | 0.87 |
| Average time per ticket | 10.3 s |
| Average tokens per ticket | 8,203 |
| Retrieval hit-rate@5 / MRR | 1 / 1.0 |

## Per scenario

| ID | Category | Urgency | Sentiment | Status | Confidence | Sources | Checks |
|---|---|---|---|---|---|---|---|
| PDF-1 | Shipping | Medium | Frustrated | Resolved | 0.88 | shipping_policy.md | ✓ |
| PDF-2 | Refund | Medium | Neutral | Resolved | 0.88 | refund_policy.md | ✓ |
| PDF-3 | Account | Medium | Neutral | Resolved | 1.00 | account_and_login.md | ✓ |
| PDF-4 | Product | Medium | Frustrated | Resolved | 0.71 | warranty_and_replacements.md | ✓ |
| PDF-5 | Billing | High | Angry | Escalated | 1.00 | billing_and_payments.md; order ORD-49012 | ✓ |
| PDF-6 | Other | Low | Neutral | Escalated | 0.00 | none | ✓ |
| OWN-1 | Product | Medium | Frustrated | Resolved | 1.00 | bat_care_and_services.md; warranty_and_replacements.md | ✓ |
| OWN-2 | Product | Medium | Neutral | Resolved | 0.82 | refund_policy.md | ✓ |
| OWN-3 | Shipping | Medium | Frustrated | Resolved | 0.94 | order ORD-50577; shipping_policy.md | ✓ |
| OWN-4 | Refund | Medium | Neutral | Resolved | 0.92 | order ORD-50689; refund_policy.md | ✓ |
| OWN-5 | Account | High | Frustrated | Escalated | 0.92 | account_and_login.md; billing_and_payments.md | ✓ |
| OWN-6 | Product | High | Frustrated | Escalated | 1.00 | warranty_and_replacements.md | ✓ |
| OWN-7 | Other | Low | Neutral | Resolved | 0.86 | team_and_academy_orders.md | ✓ |
| OWN-8 | Billing | Low | Neutral | Resolved | 1.00 | billing_and_payments.md | ✓ |
| OWN-9 | Product | Low | Neutral | Resolved | 1.00 | bat_care_and_services.md | ✓ |
| OWN-10 | Account | Medium | Frustrated | Resolved | 1.00 | account_and_login.md; resolved_cases.md | ✓ |
