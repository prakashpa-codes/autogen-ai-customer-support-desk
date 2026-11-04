"""Hybrid retrieval over the knowledge base: BM25 keyword search + FAISS vector search,
fused with Reciprocal Rank Fusion (RRF) and a soft boost for category-relevant documents.

Why hybrid: BM25 catches exact terms (SKUs, "OTP", "COD", "₹999"); embeddings catch
paraphrases ("my parcel never came" -> "order has not arrived"). RRF merges the two rank
lists without needing their scores to be on the same scale.

Why a relevance gate: every query returns *something* from a vector index. The cosine
similarity of each passage is kept so the Knowledge Agent can tell "found a policy"
apart from "nearest unrelated text", which is what drives the honest no-match path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import faiss
import numpy as np
from rank_bm25 import BM25Okapi

log = logging.getLogger(__name__)

RRF_K = 60
CATEGORY_BOOST = 0.5 / (RRF_K + 1)  # worth roughly half a first-place rank
MAX_CHUNK_CHARS = 1600
CANDIDATES = 20
MAX_PER_SOURCE = 3
MAX_CATALOGUE_CHUNKS = 2

# Which documents are the natural home for each ticket category. Used as a soft retrieval
# boost and by the confidence scorer's category-alignment check.
CATEGORY_SOURCES: dict[str, set[str]] = {
    "Shipping": {"shipping_policy.md", "orders_and_cancellations.md"},
    "Refund": {"refund_policy.md", "orders_and_cancellations.md", "warranty_and_replacements.md"},
    "Account": {"account_and_login.md"},
    "Product": {
        "product_faq.md",
        "refund_policy.md",  # size exchanges and returns of products
        "product_catalogue.md",
        "warranty_and_replacements.md",
        "bat_care_and_services.md",
    },
    "Billing": {"billing_and_payments.md", "refund_policy.md"},
    # Bulk, corporate and general company questions; only boosts ranking (the relevance gate still applies).
    "Other": {"team_and_academy_orders.md", "about_dada_sports.md"},
}

_STOPWORDS = set(
    "a an the and or but if of to in on at by for with from is are was were be been it this that "
    "these those i me my we our you your he she they them their do does did can could will would "
    "should has have had not no so as up out about into than then there here what which who how "
    "when where why all any some please hi hello dear thanks thank".split()
)


@dataclass(frozen=True)
class Chunk:
    """One retrievable section of a knowledge-base document."""

    source: str  # file name
    title: str  # document title (first H1)
    section: str  # heading path inside the document, e.g. "Return windows by category"
    text: str

    @property
    def embed_text(self) -> str:
        # Prefixing the title and section gives short sections enough context to embed well.
        return f"{self.title} - {self.section}\n{self.text}"

    @property
    def key(self) -> str:
        return hashlib.sha256(self.embed_text.encode("utf-8")).hexdigest()


@dataclass
class ScoredChunk:
    chunk: Chunk
    similarity: float  # cosine similarity to the query
    rrf_score: float
    dense_rank: int | None
    bm25_rank: int | None


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (n, d) float32 array of L2-normalised vectors."""


def _normalise(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype="float32")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


class OpenAIEmbedder:
    """OpenAI embeddings (default text-embedding-3-small), batched."""

    def __init__(self, model: str, api_key: str, batch_size: int = 96) -> None:
        from openai import OpenAI

        self.name = model
        self._client = OpenAI(api_key=api_key)
        self._batch_size = batch_size

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            response = self._client.embeddings.create(model=self.name, input=batch)
            vectors.extend(item.embedding for item in response.data)
        return _normalise(np.array(vectors))


class HashingEmbedder:
    """Deterministic bag-of-words embedder for offline tests (no API calls)."""

    name = "hashing-test"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for row, text in enumerate(texts):
            for token in tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).digest()
                out[row, int.from_bytes(digest[:4], "little") % self.dim] += 1.0
        return _normalise(out)


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9₹]+(?:-[a-z0-9]+)*", text.lower())
    return [t for t in tokens if t not in _STOPWORDS]


# --------------------------------------------------------------------------- loading


def _split_long(text: str) -> list[str]:
    """Split an oversized section on blank lines, keeping tables and lists intact."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    parts, current = [], ""
    for block in re.split(r"\n\s*\n", text):
        if current and len(current) + len(block) > MAX_CHUNK_CHARS:
            parts.append(current.strip())
            current = ""
        current += block + "\n\n"
    if current.strip():
        parts.append(current.strip())
    return parts


def chunk_markdown(source: str, markdown: str) -> list[Chunk]:
    """One chunk per H2/H3 section; the intro before the first H2 becomes an 'Overview'."""
    title = source
    h2 = "Overview"
    section = "Overview"
    buffer: list[str] = []
    chunks: list[Chunk] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            for piece in _split_long(body):
                chunks.append(Chunk(source=source, title=title, section=section, text=piece))
        buffer.clear()

    for line in markdown.splitlines():
        if line.startswith("# "):
            flush()
            title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            h2 = line[3:].strip()
            section = h2
        elif line.startswith("### "):
            flush()
            section = f"{h2} > {line[4:].strip()}"
        else:
            buffer.append(line)
    flush()
    return chunks


def chunk_pdf(path: Path) -> list[Chunk]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        for piece in _split_long(text):
            chunks.append(Chunk(source=path.name, title=path.stem, section=f"Page {number}", text=piece))
    return chunks


def load_chunks(kb_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(kb_dir.iterdir()):
        suffix = path.suffix.lower()
        if suffix in {".md", ".txt"}:
            chunks.extend(chunk_markdown(path.name, path.read_text(encoding="utf-8")))
        elif suffix == ".pdf":
            chunks.extend(chunk_pdf(path))
    return chunks


# --------------------------------------------------------------------------- index


class KnowledgeBase:
    """Loads the knowledge base, embeds it into a saved FAISS index and serves hybrid search.

    The index is saved to `index_dir` as `index.faiss` (the vectors) plus `chunks.json`
    (which chunk each vector belongs to). On the next start, chunks whose text is unchanged
    reuse their saved vector, so only new or edited sections are embedded again.
    """

    def __init__(self, kb_dir: Path, embedder: Embedder, index_dir: Path | None = None) -> None:
        self.kb_dir = kb_dir
        self.embedder = embedder
        self.index_dir = index_dir
        self.chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None
        self._index: faiss.IndexFlatIP | None = None
        self._bm25: BM25Okapi | None = None

    # -- build ---------------------------------------------------------------

    def build(self) -> "KnowledgeBase":
        chunks = load_chunks(self.kb_dir)
        if not chunks:
            raise ValueError(f"No .md, .txt or .pdf documents found in {self.kb_dir}")
        saved = self._load_saved_vectors()
        changed = set(saved) != {c.key for c in chunks}  # sections added, edited or removed
        missing = [c for c in chunks if c.key not in saved]
        if missing:
            log.info("Embedding %d new or changed chunks", len(missing))
            for chunk, vector in zip(missing, self.embedder.embed([c.embed_text for c in missing])):
                saved[chunk.key] = vector

        vectors = np.stack([saved[c.key] for c in chunks]).astype("float32")
        index = faiss.IndexFlatIP(vectors.shape[1])  # inner product of unit vectors = cosine similarity
        index.add(vectors)
        if changed:
            self._save(index, chunks)
        self.chunks, self._vectors, self._index = chunks, vectors, index
        self._bm25 = BM25Okapi([tokenize(c.embed_text) for c in chunks])
        log.info("Knowledge base ready: %d chunks from %d documents", len(chunks), len(self.sources()))
        return self

    def _load_saved_vectors(self) -> dict[str, np.ndarray]:
        """Vectors from the saved index, keyed by chunk hash (empty if none or another embedder)."""
        if self.index_dir is None or not (self.index_dir / "chunks.json").exists():
            return {}
        meta = json.loads((self.index_dir / "chunks.json").read_text(encoding="utf-8"))
        if meta.get("embedding_model") != self.embedder.name:
            return {}
        index = faiss.read_index(str(self.index_dir / "index.faiss"))
        vectors = index.reconstruct_n(0, index.ntotal)
        return {entry["key"]: vector for entry, vector in zip(meta["chunks"], vectors)}

    def _save(self, index: faiss.Index, chunks: list[Chunk]) -> None:
        if self.index_dir is None:
            return
        self.index_dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(self.index_dir / "index.faiss"))
        meta = {
            "embedding_model": self.embedder.name,
            "chunks": [{"key": c.key, "source": c.source, "section": c.section} for c in chunks],
        }
        (self.index_dir / "chunks.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")

    # -- query ---------------------------------------------------------------

    def similarity_to(self, text: str, chunks: list[Chunk]) -> list[float]:
        """Cosine similarity between a free text (e.g. the customer's request) and indexed chunks."""
        if self._vectors is None:
            raise RuntimeError("KnowledgeBase.build() must be called first")
        position = {c.key: i for i, c in enumerate(self.chunks)}
        vector = self.embedder.embed([text])[0]
        return [float(self._vectors[position[c.key]] @ vector) if c.key in position else 0.0 for c in chunks]

    def sources(self) -> list[str]:
        return sorted({c.source for c in self.chunks})

    def search(self, query: str, category: str | None = None, k: int = 5) -> list[ScoredChunk]:
        """Hybrid search. Returns the top-k chunks by fused score, each with its cosine similarity."""
        if self._index is None or self._bm25 is None or self._vectors is None:
            raise RuntimeError("KnowledgeBase.build() must be called before search()")

        query_vector = self.embedder.embed([query])
        similarities = (self._vectors @ query_vector[0]).astype(float)

        n = min(CANDIDATES, len(self.chunks))
        _, dense_ids = self._index.search(query_vector, n)
        dense_rank = {int(i): r for r, i in enumerate(dense_ids[0]) if i >= 0}

        bm25_scores = self._bm25.get_scores(tokenize(query))
        bm25_order = [int(i) for i in np.argsort(-bm25_scores)[:n] if bm25_scores[i] > 0]
        bm25_rank = {i: r for r, i in enumerate(bm25_order)}

        preferred = CATEGORY_SOURCES.get(category or "", set())
        fused: dict[int, float] = {}
        for i in set(dense_rank) | set(bm25_rank):
            score = 0.0
            if i in dense_rank:
                score += 1.0 / (RRF_K + dense_rank[i] + 1)
            if i in bm25_rank:
                score += 1.0 / (RRF_K + bm25_rank[i] + 1)
            if self.chunks[i].source in preferred:
                score += CATEGORY_BOOST
            fused[i] = score

        # Diversify: the catalogue's many near-identical product entries are capped hardest, so they
        # cannot crowd the governing policy out of the top k; policies keep up to MAX_PER_SOURCE sections.
        ranked, per_source = [], {}
        for i in sorted(fused, key=fused.get, reverse=True):
            source = self.chunks[i].source
            limit = MAX_CATALOGUE_CHUNKS if source == "product_catalogue.md" else MAX_PER_SOURCE
            if per_source.get(source, 0) < limit:
                ranked.append(i)
                per_source[source] = per_source.get(source, 0) + 1
            if len(ranked) == k:
                break
        return [
            ScoredChunk(
                chunk=self.chunks[i],
                similarity=float(similarities[i]),
                rrf_score=fused[i],
                dense_rank=dense_rank.get(i),
                bm25_rank=bm25_rank.get(i),
            )
            for i in ranked
        ]
