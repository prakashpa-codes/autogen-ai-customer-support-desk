from core.confidence import WEIGHTS, assess_confidence, strip_citations
from core.schemas import Passage

REFUNDS = Passage(
    id="S1",
    source="refund_policy.md",
    section="Refund timelines",
    text="UPI and wallets: 1-3 business days. Credit and debit cards: 5-7 business days.",
    similarity=0.72,
)
SHIPPING = Passage(
    id="S2",
    source="shipping_policy.md",
    section="Shipping charges",
    text="Standard shipping is free on orders of ₹999 or more.",
    similarity=0.40,
)


def _assess(reply, cited, retrieved=None, category="Refund", customer="My order ORD-4512"):
    return assess_confidence(reply, cited, retrieved or cited, category, customer, min_relevance=0.35)


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_well_grounded_reply_scores_high():
    reply = "Hi Priya, sorry for the wait.\nCard refunds take 5-7 business days after initiation. [S1]"
    report = _assess(reply, [REFUNDS])
    assert report.components["fact_check"] == 1.0
    assert report.components["citation_coverage"] == 1.0
    assert report.score >= 0.9


def test_invented_number_lowers_fact_check():
    reply = "Card refunds take 10 business days. [S1]"  # 10 is not in the passage
    report = _assess(reply, [REFUNDS])
    assert report.components["fact_check"] == 0.0
    assert any("unverified: 10" in note for note in report.notes)


def test_uncited_factual_paragraph_lowers_coverage():
    reply = (
        "Hi Priya, thanks for writing in.\n\n"
        "Card refunds take 5-7 business days. [S1]\n\n"
        "You will also get a free gift voucher with your next order from our store.\n\n"
        "Thanks!"
    )
    report = _assess(reply, [REFUNDS])
    assert report.components["citation_coverage"] == 0.5


def test_list_numbers_and_other_scripts_are_handled():
    # "1." / "2." are list markers, not figures; Bengali ৫-৭ is the same figure as 5-7.
    reply = "Hello.\n\n1. Card refunds take ৫-৭ business days. [S1]\n2. Please keep your order ID handy. [S1]\n\nThanks!"
    report = _assess(reply, [REFUNDS])
    assert report.components["fact_check"] == 1.0
    assert report.components["citation_coverage"] == 1.0


def test_customer_supplied_numbers_are_not_policy_facts():
    reply = "Your order ORD-4512 refund to card takes 5-7 business days. [S1]"
    assert _assess(reply, [REFUNDS]).components["fact_check"] == 1.0


def test_no_citations_means_zero_confidence():
    report = _assess("We will refund you soon.", [])
    assert report.score == 0.0


def test_off_category_sources_are_penalised():
    report = _assess("Shipping is free on orders of ₹999 or more. [S2]", [SHIPPING], category="Account")
    assert report.components["category_alignment"] == 0.5


def test_weak_similarity_lowers_evidence_strength():
    strong = _assess("Card refunds take 5-7 business days. [S1]", [REFUNDS])
    weak = _assess("Shipping is free on orders of ₹999 or more. [S2]", [SHIPPING], category="Shipping")
    assert weak.components["evidence_strength"] < strong.components["evidence_strength"]


def test_strip_citations_for_customers():
    assert strip_citations("Refunds take 5-7 days [S1][S3]. Thanks [S2] !") == "Refunds take 5-7 days. Thanks!"


def test_greeting_line_and_acknowledgement_are_not_uncited_claims():
    # A real reply from the live system: only the size recommendation is a policy fact.
    sizes = Passage(id="S1", source="product_faq.md", section="Bat size chart",
                    text="4 ft 6 in - 4 ft 9 in: Size 5", similarity=0.55)
    reply = (
        "Hi Rahul,\n\n"
        "I understand you're looking for the right bat size for your son.\n\n"
        "For a height of 4 ft 8 in, the recommended bat size is Size 5 [S1].\n\n"
        "If you have any more questions or need further assistance, feel free to ask!\n\n"
        "Best regards,"
    )
    report = assess_confidence(reply, [sizes], [sizes], "Product", "My son is 4 ft 8 in tall.", min_relevance=0.35)
    assert report.components["citation_coverage"] == 1.0


def test_evidence_strength_uses_relevance_to_the_request_not_just_query_wording():
    bulk = Passage(id="S1", source="team_and_academy_orders.md", section="Bulk equipment orders",
                   text="26-50 units: 15% team discount.", similarity=0.42, request_similarity=0.62)
    report = assess_confidence("Orders of 26-50 bats get a 15% team discount. [S1]", [bulk], [bulk], "Other",
                               "40 bats for our academy", min_relevance=0.40)
    assert report.components["evidence_strength"] == 1.0  # 0.62 >= strong threshold
