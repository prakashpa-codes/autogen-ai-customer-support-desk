from core.retrieval import CATEGORY_SOURCES, chunk_markdown, tokenize


def test_markdown_is_chunked_by_section_with_title_context():
    md = "# Refund Policy\nIntro text.\n\n## Return windows\nShoes: 30 days.\n\n### Bats\nBats: 7 days.\n"
    chunks = chunk_markdown("refund_policy.md", md)
    assert [c.section for c in chunks] == ["Overview", "Return windows", "Return windows > Bats"]
    assert chunks[1].title == "Refund Policy"
    assert chunks[2].embed_text.startswith("Refund Policy - Return windows > Bats")


def test_tokenizer_keeps_rupee_amounts_and_skus():
    tokens = tokenize("Free shipping over ₹999 on DS-FIT-801!")
    assert "₹999" in tokens and "ds-fit-801" in tokens
    assert "on" not in tokens  # stopword removed


def test_every_knowledge_base_document_is_loaded(kb):
    expected = {"shipping_policy.md", "refund_policy.md", "account_and_login.md", "product_faq.md"}
    assert expected <= set(kb.sources())
    assert len(kb.sources()) >= 11


def test_exact_terms_are_found_by_bm25(kb):
    results = kb.search("DS-FIT-801 BlendGo portable blender price", "Product", k=5)
    assert any(r.chunk.source == "product_catalogue.md" and "DS-FIT-801" in r.chunk.section for r in results)
    assert any(r.bm25_rank is not None for r in results)


def test_category_boost_prefers_owning_documents(kb):
    results = kb.search("reset my password OTP not received", "Account", k=3)
    assert results[0].chunk.source in CATEGORY_SOURCES["Account"]


def test_results_carry_similarity_for_the_relevance_gate(kb):
    results = kb.search("how long do refunds take to my card", "Refund", k=5)
    assert all(-1.0 <= r.similarity <= 1.0 for r in results)
    assert results == sorted(results, key=lambda r: r.rrf_score, reverse=True)


def test_results_are_diversified_across_documents(kb):
    # Product-heavy wording would otherwise fill the top results with catalogue entries.
    results = kb.search("exchange cricket shoes for larger size", "Product", k=5)
    sources = [r.chunk.source for r in results]
    assert sources.count("product_catalogue.md") <= 2 and max(sources.count(s) for s in set(sources)) <= 3


def test_faiss_index_is_saved_and_reused(tmp_path):
    from core.retrieval import HashingEmbedder, KnowledgeBase

    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "returns.md").write_text("# Returns\n\n## Window\nReturns are accepted within 30 days.\n", encoding="utf-8")
    index_dir = tmp_path / "faiss_index"

    class CountingEmbedder(HashingEmbedder):
        calls = 0

        def embed(self, texts):
            CountingEmbedder.calls += len(texts)
            return super().embed(texts)

    KnowledgeBase(kb_dir, CountingEmbedder(), index_dir).build()
    assert (index_dir / "index.faiss").exists() and (index_dir / "chunks.json").exists()
    assert CountingEmbedder.calls == 1

    # Unchanged section: loaded from disk. New section: only that one is embedded.
    (kb_dir / "returns.md").write_text(
        "# Returns\n\n## Window\nReturns are accepted within 30 days.\n\n## Refunds\nRefunds take 5 days.\n",
        encoding="utf-8",
    )
    kb = KnowledgeBase(kb_dir, CountingEmbedder(), index_dir).build()
    assert CountingEmbedder.calls == 2
    assert kb.search("refund days", k=1)[0].chunk.section == "Refunds"
