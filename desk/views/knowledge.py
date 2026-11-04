"""Knowledge base: the indexed documents and a search playground showing hybrid retrieval scores."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.api import Backend, BackendError


def _rank(rank: int | None) -> str:
    return "-" if rank is None else str(rank + 1)  # API ranks start at 0


def render(api: Backend) -> None:
    st.title("Knowledge base")
    try:
        docs = api.kb_documents()
    except BackendError as exc:
        st.error(str(exc))
        return
    st.caption(f"{len(docs)} documents · {sum(d['sections'] for d in docs)} searchable sections · "
               f"{sum(d['words'] for d in docs):,} words")
    st.dataframe(pd.DataFrame(docs), hide_index=True, use_container_width=True)

    st.subheader("Search playground")
    st.caption("The same hybrid search the Knowledge Base Agent uses: BM25 keywords + FAISS vectors, fused with RRF.")
    with st.form("kb_search"):
        c1, c2, c3 = st.columns([3, 1, 1])
        query = c1.text_input("Query", value="refund time for card payments")
        category = c2.selectbox("Category boost", ["", "Shipping", "Refund", "Account", "Product", "Billing", "Other"])
        k = c3.number_input("Results", 1, 15, 5)
        submitted = st.form_submit_button("Search", icon=":material/search:")
    if submitted:
        try:
            results = api.kb_search(query, category or None, int(k))
        except BackendError as exc:
            st.error(str(exc))
            return
        for r in results:
            gate = "passes relevance gate" if r["above_gate"] else "below relevance gate"
            st.markdown(
                f"**{r['source']}** › {r['section']}  \n"
                f"similarity {r['similarity']:.2f} ({gate}) · RRF {r['rrf_score']:.4f} · "
                f"vector rank {_rank(r['dense_rank'])} · keyword rank {_rank(r['bm25_rank'])}"
            )
            with st.expander("Text"):
                st.markdown(r["text"])
