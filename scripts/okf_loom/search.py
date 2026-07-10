"""Layered search for OKF bundles (see docs/research.md §A).

Three backends behind one interface, **all zero-dependency**:

    :class:`LexicalBackend`      — pure-Python BM25 with field weighting.
                                   Satisfies lexical + tag search offline.
    :class:`SemanticLiteBackend` — dependency-free fuzzy-semantic ranker
                                   (token-cosine + char-trigram cosine over a
                                   weighted concept profile). The default for
                                   ``--mode semantic``. This lightweight layer
                                   handles typo/morphology candidate retrieval;
                                   broad paraphrase recall remains a legitimate
                                   optional dense-backend use case.
    :class:`HybridBackend`       — Reciprocal Rank Fusion (k=60) of
                                   Lexical + SemanticLite.

The public entry point is :func:`search_bundle`, which the CLI ``okf search``
command calls directly. Backends consume the canonical
:class:`~okf_loom.model.ContentIndex` (the Quartz "one model" pattern) so
search, graph, and listings all read the same object.

BM25 formula (per field, then weighted-summed across fields)::

    For query terms ``t`` in field ``f`` of document ``d``:

        idf(t, f)  = ln( (N - df(t, f) + 0.5) / (df(t, f) + 0.5) + 1 )

        bm25(t, d, f) = idf(t, f) * ( tf(t, d, f) * (k1 + 1) )
                                     / ( tf(t, d, f) + k1 * (1 - b + b * dl(d, f) / avgdl(f)) )

        bm25(q, d, f) = Σ_t  bm25(t, d, f)

        score(q, d)   = Σ_f  weight(f) * bm25(q, d, f)

where:

    ``N``          total number of concepts in the corpus
    ``df(t, f)``   number of concepts whose field ``f`` contains term ``t``
    ``tf(t, d, f)``term frequency of ``t`` in field ``f`` of ``d``
    ``dl(d, f)``   token length of field ``f`` of ``d`` (0 if absent)
    ``avgdl(f)``   mean of ``dl(·, f)`` over all ``N`` concepts
    ``k1``         term-frequency saturation (default 1.5)
    ``b``          length-normalization (default 0.75)

The ``+1`` inside the ``idf`` log is the standard variant that keeps ``idf``
strictly positive (so a term appearing in every document still contributes a
small positive amount rather than going negative).

Default field weights (highest signal first):

    title (5.0) > headings (3.0) > description (2.0) > body (1.0) > tags (1.0)

Scoring is deterministic: documents are scored in sorted concept-id order and
the final ranking tie-breaks by concept id, so the same ``(bundle, query)``
always yields the same ordering.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .aliases import alias_labels
from .model import Bundle, Concept, ContentIndex
from .parse import strip_markdown_for_search
from .paths import ConceptId, concept_id_from_str, concept_id_to_str


# ---------------------------------------------------------------------------
# Modes & result type
# ---------------------------------------------------------------------------


class SearchMode(str, Enum):
    """Selector for which backend/services :func:`search_bundle` should use."""

    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    TAG = "tag"
    ENTITY = "entity"
    RELATION = "relation"


@dataclass
class SearchResult:
    """A single ranked search hit.

    Attributes:
        concept_id: canonical concept id tuple (e.g. ``("tables", "users")``).
        title: display title of the concept.
        score: backend-specific relevance score (higher = better). Tag-mode
            results carry a constant score of ``1.0``.
        snippets: 0..N short excerpts (~120 chars) centred on the first
            matches; ellipsized when truncated.
        source_backend: which backend produced this hit
            (``"lexical"`` | ``"semantic-lite"`` | ``"hybrid"`` | ``"tag"``).
        matched_tags: subset of the concept's own tags that matched the query
            (populated in tag mode; empty otherwise).
        detail: optional backend evidence. Hybrid results expose component
            scores, component ranks, and the backends that matched so callers
            never have to interpret an RRF rank score as calibrated relevance.
    """

    concept_id: ConceptId
    title: str
    score: float
    snippets: list[str] = field(default_factory=list)
    source_backend: str = "lexical"
    matched_tags: list[str] = field(default_factory=list)
    detail: dict | None = None
    description: str = ""  # P2-52: concept description for richer result context

    def as_dict(self) -> dict:
        """JSON-serializable view (concept_id rendered via ``concept_id_to_str``)."""
        d = {
            "concept_id": concept_id_to_str(self.concept_id),
            "title": self.title,
            "score": self.score,
            "snippets": list(self.snippets),
            "source_backend": self.source_backend,
            "matched_tags": list(self.matched_tags),
            "description": self.description,
        }
        if self.detail is not None:
            d["detail"] = self.detail
        return d


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# A small, dependency-free English stopword set. Deliberately modest so that
# domain terms (table, user, data, event, ...) are never stripped. Shipped
# in-tree; no nltk or similar dependency.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an and are as at be been being but by for from had has have having
    he her here hers him his how i if in into is it its itself me more most
    my no nor not of off on once only or other our out over own same she so
    some such than that the their them then there these they this those
    through to too under until up very was we were what when where which
    while who whom why will with would you your yours about above after
    again against all because before below between during each few further
    do does did doing down can could should shall may might must also via
    per within without
    """.split()
)

# Unicode word runs. ``\\w`` matches CJK ideographs too, so a run of CJK
# characters becomes a single token; the CJK fallback below splits those.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on word boundaries, drop stopwords, split CJK runs.

    CJK fallback: a token that contains *no* ASCII letter/digit is treated as
    a CJK/ideographic run and is split into one-character tokens (so that
    "比特币" indexes as ``["比", "特", "币"]``). Mixed tokens like ``hello世界``
    keep ASCII letters and are left intact per the literal rule.
    """
    if not text:
        return []
    out: list[str] = []
    for raw in _WORD_RE.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        # Does this token contain any ASCII letter/digit?
        if any(c.isascii() and c.isalnum() for c in raw):
            out.append(raw)
        else:
            # Pure non-ASCII run (e.g. CJK): index per character.
            out.extend(c for c in raw if c not in _STOPWORDS)
    return out


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------


def _extract_snippets(
    text: str,
    query_terms: list[str],
    *,
    max_snippets: int = 2,
    width: int = 120,
    min_gap: int = 60,
) -> list[str]:
    """Return up to ``max_snippets`` excerpts of ``text`` centred on matches.

    Matches are found case-insensitively as substrings of any query term
    (robust for both ASCII words and CJK characters). Two snippet centres are
    kept at least ``min_gap`` characters apart so we don't show the same
    window twice. Each excerpt is ~``width`` chars, whitespace-collapsed, and
    ellipsized when it doesn't reach a string boundary.
    """
    if not text or not query_terms:
        return []
    terms = [re.escape(t) for t in query_terms if t]
    if not terms:
        return []
    pat = re.compile("|".join(terms))
    lower = text.lower()

    centres: list[int] = []
    last = -(10**9)
    for m in pat.finditer(lower):
        start = m.start()
        if start - last >= min_gap:
            centres.append(start)
            last = start
        if len(centres) >= max_snippets:
            break
    if not centres:
        return []

    half = width // 2
    snippets: list[str] = []
    n = len(text)
    for pos in centres:
        start = max(0, pos - half)
        end = min(n, start + width)
        # Re-extend the start if we hit the right edge short.
        if end - start < width:
            start = max(0, end - width)
        chunk = re.sub(r"\s+", " ", text[start:end]).strip()
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < n else ""
        snippets.append(f"{prefix}{chunk}{suffix}")
    return snippets


# ---------------------------------------------------------------------------
# Backend protocol + LexicalBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchBackend(Protocol):
    """Pluggable search backend (research.md §A plugin interface).

    A backend indexes the canonical :class:`ContentIndex` once and answers
    ranked queries. ``name`` identifies the backend on :class:`SearchResult`.
    """

    name: str

    def index(self, content_index: ContentIndex) -> None: ...

    def search(self, query: str, *, limit: int) -> list[SearchResult]: ...


DEFAULT_FIELD_WEIGHTS: dict[str, float] = {
    "title": 5.0,
    "headings": 3.0,
    "aliases": 3.0,
    "description": 2.0,
    "body": 1.0,
    "tags": 1.0,
}


class LexicalBackend:
    """Always-available BM25 backend with field weighting. Pure Python, zero deps.

    Args:
        k1: BM25 term-frequency saturation (default 1.5).
        b: BM25 length normalization (default 0.75).
        field_weights: optional override/extension of :data:`DEFAULT_FIELD_WEIGHTS`.
            Unknown field names are tolerated (treated as empty per concept).
    """

    name = "lexical"

    def __init__(
        self,
        *,
        k1: float = 1.5,
        b: float = 0.75,
        field_weights: dict[str, float] | None = None,
    ) -> None:
        self.k1 = k1
        self.b = b
        if field_weights is None:
            self._field_weights = dict(DEFAULT_FIELD_WEIGHTS)
        else:
            merged = dict(DEFAULT_FIELD_WEIGHTS)
            merged.update(field_weights)
            self._field_weights = merged
        self._fields: tuple[str, ...] = tuple(self._field_weights.keys())

        self._indexed: bool = False
        self._doc_ids: list[ConceptId] = []
        self._N: int = 0
        self._tf: dict[str, dict[ConceptId, Counter]] = {}
        self._df: dict[str, dict[str, int]] = {}
        self._dl: dict[str, dict[ConceptId, int]] = {}
        self._avgdl: dict[str, float] = {}
        self._concepts: dict[ConceptId, Concept] = {}
        self._body_text: dict[ConceptId, str] = {}
        self._desc_text: dict[ConceptId, str] = {}

    # -- indexing -----------------------------------------------------------

    @staticmethod
    def _field_texts(concept: Concept) -> dict[str, str]:
        return {
            "title": concept.title or "",
            "headings": " ".join(h.text for h in concept.headings),
            "aliases": " ".join(alias_labels(concept.frontmatter.get("aliases"))),
            "description": concept.description or "",
            "body": strip_markdown_for_search(concept.body or ""),
            "tags": " ".join(concept.tags),
        }

    def index(self, content_index: ContentIndex) -> None:
        """Build the per-field BM25 statistics from the canonical content index."""
        by_id = content_index.by_id
        doc_ids = sorted(by_id.keys())
        self._doc_ids = doc_ids
        self._N = len(doc_ids)
        self._concepts = dict(by_id)

        tf: dict[str, dict[ConceptId, Counter]] = {f: {} for f in self._fields}
        df: dict[str, dict[str, int]] = {f: {} for f in self._fields}
        dl: dict[str, dict[ConceptId, int]] = {f: {} for f in self._fields}
        body_text: dict[ConceptId, str] = {}
        desc_text: dict[ConceptId, str] = {}

        for cid in doc_ids:
            concept = by_id[cid]
            field_texts = self._field_texts(concept)
            body_text[cid] = field_texts.get("body", "")
            desc_text[cid] = field_texts.get("description", "")
            for f in self._fields:
                toks = tokenize(field_texts.get(f, ""))
                if not toks:
                    continue
                counter = Counter(toks)
                tf[f][cid] = counter
                dl[f][cid] = len(toks)
                for term in counter:  # distinct terms only
                    df[f][term] = df[f].get(term, 0) + 1

        avgdl: dict[str, float] = {}
        for f in self._fields:
            total = sum(dl[f].values())
            avgdl[f] = (total / self._N) if self._N else 0.0

        self._tf = tf
        self._df = df
        self._dl = dl
        self._avgdl = avgdl
        self._body_text = body_text
        self._desc_text = desc_text
        self._indexed = True

    # -- querying -----------------------------------------------------------

    def _score_doc(self, cid: ConceptId, q_terms: list[str]) -> float:
        """Weighted-sum BM25 score for one document against the distinct query terms."""
        score = 0.0
        N = self._N
        k1 = self.k1
        b = self.b
        for f in self._fields:
            weight = self._field_weights[f]
            tf_doc = self._tf[f].get(cid)
            if not tf_doc:
                continue
            avgdl = self._avgdl[f]
            dl = self._dl[f][cid]
            norm_dl = (dl / avgdl) if avgdl > 0 else 0.0
            denom_base = k1 * (1.0 - b + b * norm_dl)
            df_field = self._df[f]
            bm25_f = 0.0
            for t in q_terms:
                tf_t = tf_doc.get(t, 0)
                if tf_t == 0:
                    continue
                df_t = df_field.get(t, 0)
                if df_t == 0:
                    continue
                idf = math.log((N - df_t + 0.5) / (df_t + 0.5) + 1.0)
                bm25_f += idf * (tf_t * (k1 + 1.0)) / (tf_t + denom_base)
            score += weight * bm25_f
        return score

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        if not self._indexed:
            return []
        # Distinct query terms (order preserved for stable iteration).
        seen: set[str] = set()
        q_terms: list[str] = []
        for t in tokenize(query):
            if t not in seen:
                seen.add(t)
                q_terms.append(t)
        if not q_terms:
            return []

        scored: list[tuple[float, ConceptId]] = []
        for cid in self._doc_ids:
            s = self._score_doc(cid, q_terms)
            if s > 0.0:
                scored.append((s, cid))
        # Highest score first; concept id as a deterministic tie-break.
        scored.sort(key=lambda x: (-x[0], x[1]))

        cap = limit if limit and limit > 0 else len(scored)
        results: list[SearchResult] = []
        for score, cid in scored[:cap]:
            concept = self._concepts[cid]
            snips = _extract_snippets(self._body_text[cid], q_terms)
            if not snips:
                # Fall back to the description when the body had no match.
                snips = _extract_snippets(self._desc_text[cid], q_terms)
            results.append(
                SearchResult(
                    concept_id=cid,
                    title=concept.title,
                    score=score,
                    snippets=snips,
                    source_backend="lexical",
                    matched_tags=[],
                    detail={
                        "matched_backends": ["lexical"],
                        "component_scores": {"lexical": score},
                    },
                )
            )
        return results


# ---------------------------------------------------------------------------
# SemanticLiteBackend — dependency-free fuzzy-semantic ranker (current spec §6)
# ---------------------------------------------------------------------------


def _char_trigrams(text: str) -> Counter:
    """Character trigram multiset over a lowercased, whitespace-collapsed string.

    Each token is padded with a leading/trailing space. E.g. ``orders`` →
    `` or``, ``ord``, ``rde``, ``der``, ``ers``, ``rs ``.
    """
    if not text:
        return Counter()
    collapsed = re.sub(r"\s+", " ", text.lower()).strip()
    if not collapsed:
        return Counter()
    counts: Counter = Counter()
    for token in collapsed.split():
        padded = f" {token} "
        for i in range(len(padded) - 2):
            counts[padded[i : i + 3]] += 1
    return counts


def _cosine_sparse(a: Counter, b: Counter) -> float:
    """Cosine similarity over two sparse TF dicts (Counters)."""
    if not a or not b:
        return 0.0
    # Iterate over the smaller dict for efficiency.
    if len(a) > len(b):
        a, b = b, a
    dot = sum(count * b.get(key, 0) for key, count in a.items())
    if dot == 0:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class SemanticLiteBackend:
    """Dependency-free fuzzy-semantic ranker (current spec §6).

    **Not embeddings.** Combines token-level TF cosine (shares the lexical
    tokenizer) with character-trigram TF cosine (robust to morphology/typos).
    Score = ``α * cosine(tokens) + (1-α) * cosine(trigrams)``, default ``α=0.6``.

    The per-concept *profile string* weights title (×3) and description (×2)
    plus type, headings, tags, and — if ``okf.cap.entities`` /
    ``okf.cap.aliases`` are active — entity labels and aliases.
    """

    name = "semantic-lite"

    def __init__(self, *, token_weight: float = 0.6) -> None:
        self.token_weight = token_weight
        self._indexed = False
        self._doc_ids: list[ConceptId] = []
        self._concepts: dict[ConceptId, Concept] = {}
        self._token_tf: dict[ConceptId, Counter] = {}
        self._trigram_tf: dict[ConceptId, Counter] = {}
        self._body_text: dict[ConceptId, str] = {}
        self._desc_text: dict[ConceptId, str] = {}

    # -- profile construction ------------------------------------------------

    @staticmethod
    def _profile_string(concept: Concept) -> str:
        """Build the weighted profile string for fuzzy-semantic matching."""
        parts: list[str] = []
        # Title ×3
        title = concept.title or ""
        for _ in range(3):
            parts.append(title)
        # Description ×2
        desc = concept.description or ""
        for _ in range(2):
            parts.append(desc)
        # Type
        if concept.type:
            parts.append(concept.type)
        # All heading texts
        for h in concept.headings:
            parts.append(h.text)
        # Tags
        parts.extend(concept.tags)
        # Entity labels + aliases (if entities capability active)
        entities = concept.frontmatter.get("entities")
        if entities and isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, str):
                    parts.append(ent)
                elif isinstance(ent, dict):
                    label = ent.get("label")
                    if label:
                        parts.append(label)
                    ent_aliases = ent.get("aliases")
                    if isinstance(ent_aliases, list):
                        parts.extend(a for a in ent_aliases if isinstance(a, str))
        # Top-level aliases (if aliases capability active)
        parts.extend(alias_labels(concept.frontmatter.get("aliases")))
        return " ".join(parts)

    # -- indexing ------------------------------------------------------------

    def index(self, content_index: ContentIndex) -> None:
        by_id = content_index.by_id
        doc_ids = sorted(by_id.keys())
        self._doc_ids = doc_ids
        self._concepts = dict(by_id)
        self._token_tf = {}
        self._trigram_tf = {}
        self._body_text = {}
        self._desc_text = {}
        for cid in doc_ids:
            concept = by_id[cid]
            profile = self._profile_string(concept)
            self._token_tf[cid] = Counter(tokenize(profile))
            self._trigram_tf[cid] = _char_trigrams(profile)
            self._body_text[cid] = strip_markdown_for_search(concept.body or "")
            self._desc_text[cid] = concept.description or ""
        self._indexed = True

    # -- querying ------------------------------------------------------------

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        if not self._indexed:
            return []
        q_token_tf = Counter(tokenize(query))
        q_trigram_tf = _char_trigrams(query)
        alpha = self.token_weight

        scored: list[tuple[float, ConceptId, float, float]] = []
        for cid in self._doc_ids:
            tok_sim = _cosine_sparse(q_token_tf, self._token_tf[cid])
            tri_sim = _cosine_sparse(q_trigram_tf, self._trigram_tf[cid])
            score = alpha * tok_sim + (1.0 - alpha) * tri_sim
            if score > 0.0:
                scored.append((score, cid, tok_sim, tri_sim))

        scored.sort(key=lambda x: (-x[0], x[1]))
        cap = limit if limit and limit > 0 else len(scored)
        results: list[SearchResult] = []
        q_terms = list(q_token_tf.keys())
        for score, cid, tok_sim, tri_sim in scored[:cap]:
            concept = self._concepts[cid]
            snips = _extract_snippets(self._body_text[cid], q_terms)
            if not snips:
                snips = _extract_snippets(self._desc_text[cid], q_terms)
            results.append(
                SearchResult(
                    concept_id=cid,
                    title=concept.title,
                    score=score,
                    snippets=snips,
                    source_backend="semantic-lite",
                    detail={
                        "matched_backends": ["semantic-lite"],
                        "component_scores": {
                            "semantic-lite": score,
                            "token_cosine": tok_sim,
                            "trigram_cosine": tri_sim,
                        },
                    },
                )
            )
        return results


# ---------------------------------------------------------------------------
# HybridBackend — RRF fusion (current spec §6)
# ---------------------------------------------------------------------------


class HybridBackend:
    """Hybrid fusion via Reciprocal Rank Fusion (current spec §6).

    Fuses :class:`LexicalBackend` and :class:`SemanticLiteBackend` (both
    zero-dependency) so hybrid works dependency-free.

    RRF: for each concept, ``rrf = Σ_backend 1/(k + rank_backend)``, ``k=60``.
    Concepts missing from a backend contribute 0 from that backend. Sort by
    ``(-rrf, concept_id)``.
    """

    name = "hybrid"

    def __init__(
        self,
        lexical: SearchBackend,
        semantic: SearchBackend,
        *,
        k: int = 60,
        semantic_min_score: float | None = None,
        require_backend: str = "any",
    ) -> None:
        self._lexical = lexical
        self._semantic = semantic
        self.k = k
        if semantic_min_score is not None and not 0.0 <= semantic_min_score <= 1.0:
            raise ValueError("semantic_min_score must be between 0.0 and 1.0")
        if require_backend not in {"any", "lexical", "semantic", "both"}:
            raise ValueError(
                "require_backend must be one of: any, lexical, semantic, both"
            )
        self.semantic_min_score = semantic_min_score
        self.require_backend = require_backend

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        n = max(limit * 5, 50)
        lex_results = self._lexical.search(query, limit=n)
        sem_results = self._semantic.search(query, limit=n)
        if self.semantic_min_score is not None:
            sem_results = [
                r for r in sem_results if r.score >= self.semantic_min_score
            ]

        # Build rank maps (1-indexed).
        lex_rank: dict[ConceptId, int] = {}
        for i, r in enumerate(lex_results):
            lex_rank[r.concept_id] = i + 1
        sem_rank: dict[ConceptId, int] = {}
        for i, r in enumerate(sem_results):
            sem_rank[r.concept_id] = i + 1

        all_ids = set(lex_rank.keys()) | set(sem_rank.keys())
        if self.require_backend == "lexical":
            all_ids &= set(lex_rank)
        elif self.require_backend == "semantic":
            all_ids &= set(sem_rank)
        elif self.require_backend == "both":
            all_ids &= set(lex_rank) & set(sem_rank)
        k = self.k
        scored: list[tuple[float, ConceptId]] = []
        # P3-5: build the cid → title map ONCE in O(N). The previous loop
        # re-scanned (*lex_results, *sem_results) for every cid and relied
        # on a misleading ``break`` that only exited the inner-most loop
        # (O(N²) with the same final result). A single comprehension is
        # behaviour-preserving and asymptotically linear. NOTE: dict
        # comprehension keeps the LATER value on duplicate keys, so for a
        # concept ranked by BOTH backends the SEMANTIC title wins — this
        # matches the original code's accidental behaviour (the misleading
        # ``break`` did not exit the outer loop, so semantic overwrote
        # lexical). snippet_map below uses explicit ``not in`` so it keeps
        # LEXICAL precedence; the two intentionally differ, unchanged.
        title_map: dict[ConceptId, str] = {
            r.concept_id: r.title for r in (*lex_results, *sem_results)
        }
        # P1-11: forward snippets from whichever fused backend ranked the
        # concept. Check lexical first (precedence), then semantic, so the
        # fused result carries non-empty snippets when at least one backend
        # produced them.
        snippet_map: dict[ConceptId, list[str]] = {}
        lex_by_id = {r.concept_id: r for r in lex_results}
        sem_by_id = {r.concept_id: r for r in sem_results}
        for cid in all_ids:
            rrf = 0.0
            if cid in lex_rank:
                rrf += 1.0 / (k + lex_rank[cid])
            if cid in sem_rank:
                rrf += 1.0 / (k + sem_rank[cid])
            scored.append((rrf, cid))

        # Forward snippets: lexical precedence, then semantic fallback.
        for results in (lex_results, sem_results):
            for r in results:
                if r.concept_id not in snippet_map:
                    snippet_map[r.concept_id] = list(r.snippets)

        scored.sort(key=lambda x: (-x[0], x[1]))
        cap = limit if limit and limit > 0 else len(scored)
        results: list[SearchResult] = []
        for rrf, cid in scored[:cap]:
            matched_backends: list[str] = []
            component_scores: dict[str, float] = {}
            component_ranks: dict[str, int] = {}
            if cid in lex_by_id:
                matched_backends.append("lexical")
                component_scores["lexical"] = lex_by_id[cid].score
                component_ranks["lexical"] = lex_rank[cid]
            if cid in sem_by_id:
                matched_backends.append("semantic-lite")
                component_scores["semantic-lite"] = sem_by_id[cid].score
                component_ranks["semantic-lite"] = sem_rank[cid]
            results.append(
                SearchResult(
                    concept_id=cid,
                    title=title_map.get(cid, ""),
                    score=rrf,
                    snippets=snippet_map.get(cid, []),
                    source_backend="hybrid",
                    detail={
                        "matched_backends": matched_backends,
                        "component_scores": component_scores,
                        "component_ranks": component_ranks,
                        "fusion": "rrf",
                    },
                )
            )
        return results


# ---------------------------------------------------------------------------
# Per-bundle backend cache
# ---------------------------------------------------------------------------

# Per-bundle lexical backend cache, stored as a Bundle attribute
# (``bundle._lexical_backend``). This replaces the former module-global
# ``_LEXICAL_BACKEND_CACHE`` keyed by ``id(bundle)`` which could leak
# memory across reloads in a long-running server. The per-bundle attribute
# is dropped when the Bundle is GC'd; ``Bundle.invalidate()`` resets it.
#
# ``clear_search_cache()`` is retained for backward compatibility as a no-op
# (the old module-global no longer exists); callers that used it should
# switch to ``bundle.invalidate()``.


def _get_lexical_backend(bundle: Bundle) -> "LexicalBackend":
    """Return (creating if needed) the cached ``LexicalBackend`` for ``bundle``.

    The backend is stored on the bundle itself (``bundle._lexical_backend``)
    so it is automatically released when the bundle is GC'd, and is
    invalidated by ``bundle.invalidate()``.
    """
    if bundle._lexical_backend is None:
        bundle._lexical_backend = LexicalBackend()
    return bundle._lexical_backend


def _get_semantic_lite_backend(bundle: Bundle) -> "SemanticLiteBackend":
    """Return (creating if needed) the cached ``SemanticLiteBackend`` for ``bundle``."""
    if bundle._semantic_lite_backend is None:
        bundle._semantic_lite_backend = SemanticLiteBackend()
    return bundle._semantic_lite_backend


def clear_search_cache() -> None:
    """Deprecated no-op.

    The lexical backend cache is now per-bundle (``bundle._lexical_backend``).
    Call ``bundle.invalidate()`` to reset it. This function is retained for
    backward compatibility with existing callers and tests.
    """
    pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def mark_hybrid_active(bundle: Bundle) -> None:
    """Mark ``okf.cap.search_hybrid`` active for ``bundle`` (current spec §6).

    ``okf.cap.search_hybrid`` is a ``recommended``-tier capability with no
    governing frontmatter key, so it cannot auto-activate from observed keys.
    After a successful hybrid query we inject it into the bundle's resolved
    capabilities so ``bundle.capabilities().is_active(...)`` reports it.

    Search.py-only implementation (no model.py change): ``Bundle`` has no
    ``__slots__``, so we replace the memoized ``bundle._capabilities`` with a
    new :class:`ResolvedCapabilities` whose ``active`` dict includes the cap.
    A subsequent ``bundle.invalidate()`` drops it (and a later hybrid query
    re-activates it), which is the correct "recompute from scratch" semantics.
    """
    caps = bundle.capabilities()  # ensure computed (memoized)
    if caps.is_active("okf.cap.search_hybrid"):
        return
    from .extensions import default_registry, ResolvedCapabilities

    cap = default_registry().get("okf.cap.search_hybrid")
    if cap is None:
        # Capability unknown (registry stripped in some test); nothing to do.
        return
    new_active = dict(caps.active)
    new_active["okf.cap.search_hybrid"] = cap
    bundle._capabilities = ResolvedCapabilities(
        active=new_active, undeclared=list(caps.undeclared)
    )


def search_bundle(
    bundle: Bundle,
    query: str,
    *,
    mode: SearchMode = SearchMode.LEXICAL,
    type_filter: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    relation: str | None = None,
    source: str | None = None,
    target: str | None = None,
    semantic_min_score: float | None = None,
    hybrid_require: str = "any",
) -> list[SearchResult]:
    """Search ``bundle`` and return ranked :class:`SearchResult` objects.

    Args:
        bundle: a loaded OKF :class:`~okf_loom.model.Bundle`.
        query: search string. In tag mode this may be a bare tag (``"badges"``)
            or ``#badges`` form. In relation mode, query is optional when
            edge filters (relation/source/target) are provided.
        mode: one of :class:`SearchMode`.
        type_filter: optional post-filter — keep only concepts whose ``type``
            frontmatter equals this string (applied in every mode).
        tag: optional post-filter — keep only concepts carrying this tag
            (case-insensitive; applied in every mode).
        limit: maximum number of results to return.
        relation: relation type filter (relation mode only).
        source: source concept-id filter (relation mode only).
        target: target concept-id filter (relation mode only).
        semantic_min_score: opt-in SemanticLite cosine threshold in ``[0,1]``.
            In hybrid mode this gates the semantic component before RRF.
        hybrid_require: hybrid evidence policy: ``any`` (legacy union),
            ``lexical``, ``semantic``, or ``both``.

    Returns:
        Ranked list of :class:`SearchResult` (highest score first).

    All three backends (:class:`LexicalBackend`, :class:`SemanticLiteBackend`,
    :class:`HybridBackend`) are zero-dependency, so there are no provider
    switches. ``mode`` selects which runs: ``lexical`` → LexicalBackend,
    ``semantic`` → SemanticLiteBackend, ``hybrid`` → HybridBackend (RRF fusion
    of the two). SemanticLite is a cheap fuzzy candidate layer, not dense
    semantic retrieval; a downstream agent can rerank returned candidates but
    cannot recover a relevant concept that retrieval omitted.
    """
    if semantic_min_score is not None and not 0.0 <= semantic_min_score <= 1.0:
        raise ValueError("semantic_min_score must be between 0.0 and 1.0")
    if hybrid_require not in {"any", "lexical", "semantic", "both"}:
        raise ValueError("hybrid_require must be one of: any, lexical, semantic, both")
    if semantic_min_score is not None and mode not in {
        SearchMode.SEMANTIC, SearchMode.HYBRID
    }:
        raise ValueError("semantic_min_score is only valid in semantic or hybrid mode")
    if hybrid_require != "any" and mode != SearchMode.HYBRID:
        raise ValueError("hybrid_require is only valid in hybrid mode")

    content_index = bundle.content_index()

    if mode == SearchMode.TAG:
        return _populate_description(
            _search_tag(
                content_index,
                query,
                type_filter=type_filter,
                tag=tag,
                limit=limit,
            ),
            content_index,
        )

    if mode == SearchMode.ENTITY:
        return _populate_description(
            _search_entity(
                bundle,
                content_index,
                query,
                type_filter=type_filter,
                tag=tag,
                limit=limit,
            ),
            content_index,
        )

    if mode == SearchMode.RELATION:
        return _populate_description(
            _search_relation(
                bundle,
                content_index,
                relation=relation,
                source=source,
                target=target,
                type_filter=type_filter,
                tag=tag,
                limit=limit,
            ),
            content_index,
        )

    if mode == SearchMode.SEMANTIC:
        backend = _get_semantic_lite_backend(bundle)
        if not getattr(backend, "_indexed", False):
            backend.index(content_index)
        corpus_size = max(len(content_index.by_id), 1)
        raw = backend.search(query, limit=corpus_size)
        if semantic_min_score is not None:
            raw = [r for r in raw if r.score >= semantic_min_score]
        return _apply_filters(raw, content_index, type_filter, tag, limit)

    if mode == SearchMode.HYBRID:
        # §4.2: hybrid fuses LexicalBackend + SemanticLiteBackend (zero-dep).
        lex = _get_lexical_backend(bundle)
        if not getattr(lex, "_indexed", False):
            lex.index(content_index)
        sem = _get_semantic_lite_backend(bundle)
        if not sem._indexed:
            sem.index(content_index)
        hybrid = HybridBackend(
            lex,
            sem,
            semantic_min_score=semantic_min_score,
            require_backend=hybrid_require,
        )
        corpus_size = max(len(content_index.by_id), 1)
        raw = hybrid.search(query, limit=corpus_size)
        # Current spec §6: a successful hybrid query activates
        # okf.cap.search_hybrid for this bundle.
        # P3-2: gate on ``if raw:`` — activating a capability on a
        # zero-match query is semantically loose. The cap signals
        # "this bundle has been queried in hybrid mode and produced
        # useful results," which a zero-match does not satisfy.
        if raw:
            mark_hybrid_active(bundle)
        return _apply_filters(raw, content_index, type_filter, tag, limit)

    # --- lexical (default) ---
    backend = _get_lexical_backend(bundle)
    if not getattr(backend, "_indexed", False):
        backend.index(content_index)

    # Fetch every positive-scoring hit (backend caps at corpus size), then
    # apply the cross-cutting type/tag post-filters, then trim to ``limit``.
    corpus_size = max(len(content_index.by_id), 1)
    raw = backend.search(query, limit=corpus_size)
    return _apply_filters(raw, content_index, type_filter, tag, limit)


def _apply_filters(
    raw: list[SearchResult],
    content_index: ContentIndex,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Apply type/tag post-filters and trim to ``limit``.

    Also back-fills ``title`` (P1-6) and ``description`` (P2-52) from the
    content index so every backend's results expose the same user-visible
    shape.
    """
    tag_lc = tag.lower() if tag else None
    filtered: list[SearchResult] = []
    for r in raw:
        concept = content_index.by_id.get(r.concept_id)
        if concept is None:
            continue
        if type_filter is not None and concept.type != type_filter:
            continue
        if tag_lc is not None and tag_lc not in {t.lower() for t in concept.tags}:
            continue
        # P1-6: back-fill the concept title in case a backend emitted a
        # raw/placeholder title. Don't overwrite a real title with an empty
        # concept.title fallback (defensive — concept.title always returns
        # at least the last id segment).
        if concept.title:
            r.title = concept.title
        # P2-52: populate description from the content index so callers/server
        # get richer result context without a fabricated empty value.
        if not r.description and concept.description:
            r.description = concept.description
        filtered.append(r)
    return filtered[:limit]


def _populate_description(
    results: list[SearchResult],
    content_index: ContentIndex,
) -> list[SearchResult]:
    """Back-fill ``SearchResult.description`` from the content index (P2-19).

    TAG/ENTITY/RELATION helpers apply type/tag filters inline (so they do
    not route through :func:`_apply_filters`). Without this back-fill,
    their results carry an empty ``description`` field, which is an
    inconsistency in the :class:`SearchResult` shape across modes. This
    helper mirrors the description back-fill that ``_apply_filters``
    performs for the lexical/semantic/hybrid paths.
    """
    for r in results:
        if r.description:
            continue
        concept = content_index.by_id.get(r.concept_id)
        if concept is not None and concept.description:
            r.description = concept.description
    return results


def _search_tag(
    content_index: ContentIndex,
    query: str,
    *,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Tag-mode search: match ``Concept.tags`` case-insensitively, sort by title."""
    needle = query.lstrip("#").strip().lower()
    tag_filter_lc = tag.lower() if tag else None

    results: list[SearchResult] = []
    for cid in sorted(content_index.by_id.keys()):
        concept = content_index.by_id[cid]
        if type_filter is not None and concept.type != type_filter:
            continue
        own_tags = concept.tags
        if tag_filter_lc is not None and tag_filter_lc not in {
            t.lower() for t in own_tags
        }:
            continue
        if not needle:
            # An empty tag query still lists concepts passing the filters.
            matched: list[str] = []
        else:
            matched = [t for t in own_tags if t.lower() == needle]
            if not matched:
                continue
        results.append(
            SearchResult(
                concept_id=cid,
                title=concept.title,
                score=1.0,
                snippets=[],
                source_backend="tag",
                matched_tags=matched,
            )
        )
    # Tag mode is ordered by title (stable on concept id as secondary key).
    results.sort(key=lambda r: (r.title, r.concept_id))
    return results[:limit]


# ---------------------------------------------------------------------------
# Entity search mode (current spec §6)
# ---------------------------------------------------------------------------


def _entity_match_fields(concept: Concept) -> dict[str, str]:
    """Return the set of searchable fields for entity-mode matching."""
    fields: dict[str, str] = {
        "id": concept_id_to_str(concept.id),
        "title": concept.title or "",
        "description": concept.description or "",
    }
    # Entity labels + aliases (if entities capability active)
    entities = concept.frontmatter.get("entities")
    if entities and isinstance(entities, list):
        labels: list[str] = []
        for ent in entities:
            if isinstance(ent, str):
                labels.append(ent)
            elif isinstance(ent, dict):
                label = ent.get("label")
                if label:
                    labels.append(label)
                ent_aliases = ent.get("aliases")
                if isinstance(ent_aliases, list):
                    labels.extend(a for a in ent_aliases if isinstance(a, str))
        if labels:
            fields["entities"] = " ".join(labels)
    # Top-level aliases
    aliases = alias_labels(concept.frontmatter.get("aliases"))
    if aliases:
        fields["aliases"] = " ".join(aliases)
    return fields


# Field weights for entity-mode score (title/alias > description > id)
_ENTITY_FIELD_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "entities": 3.0,
    "aliases": 3.0,
    "description": 2.0,
    "id": 1.0,
}


def _search_entity(
    bundle: Bundle,
    content_index: ContentIndex,
    query: str,
    *,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Entity-mode search: case-insensitive substring match against concept fields."""
    needle = query.lower().strip() if query else ""
    tag_lc = tag.lower() if tag else None

    scored: list[tuple[float, ConceptId, list[str]]] = []
    for cid in sorted(content_index.by_id.keys()):
        concept = content_index.by_id[cid]
        if type_filter is not None and concept.type != type_filter:
            continue
        if tag_lc is not None and tag_lc not in {t.lower() for t in concept.tags}:
            continue
        fields = _entity_match_fields(concept)
        score = 0.0
        matched_snippets: list[str] = []
        for fname, fval in fields.items():
            if needle and needle in fval.lower():
                weight = _ENTITY_FIELD_WEIGHTS.get(fname, 1.0)
                score += weight
                # Record a short snippet of the matched field
                idx = fval.lower().index(needle)
                start = max(0, idx - 30)
                end = min(len(fval), idx + len(needle) + 30)
                snippet = fval[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(fval):
                    snippet = snippet + "…"
                matched_snippets.append(f"[{fname}] {snippet}")
        if score > 0.0:
            scored.append((score, cid, matched_snippets))

    scored.sort(key=lambda x: (-x[0], x[1]))
    results: list[SearchResult] = []
    for score, cid, snippets in scored[:limit]:
        concept = content_index.by_id[cid]
        # Build detail with matched entity info
        fields = _entity_match_fields(concept)
        matched_fields = {
            fname: fval for fname, fval in fields.items()
            if needle and needle in fval.lower()
        } if needle else {}
        results.append(SearchResult(
            concept_id=cid,
            title=concept.title,
            score=score,
            # P2-5 (iter-3): entity snippets ARE the result data (matched
            # fields). Do not cap here; the CLI text mode shows all of them.
            snippets=snippets,
            source_backend="entity",
            detail={"matched_fields": matched_fields} if matched_fields else None,
        ))
    return results


# ---------------------------------------------------------------------------
# Relation search mode (current spec §6)
# ---------------------------------------------------------------------------


def _search_relation(
    bundle: Bundle,
    content_index: ContentIndex,
    *,
    relation: str | None,
    source: str | None,
    target: str | None,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Relation-mode search: query typed-relation + markdown edges.

    Per current spec §6, ``--relation TYPE`` filters ONLY typed-relation edges
    (not markdown links whose label happens to match).
    """
    from .paths import concept_id_from_str, ConceptIdError

    graph = bundle.graph()
    tag_lc = tag.lower() if tag else None
    rel_lc = relation.lower() if relation else None

    # Parse source/target filters with safe error handling
    source_cid = None
    target_cid = None
    if source:
        try:
            source_cid = concept_id_from_str(source)
        except (ConceptIdError, ValueError):
            return []
    if target:
        try:
            target_cid = concept_id_from_str(target)
        except (ConceptIdError, ValueError):
            return []

    # Collect matching edges grouped by source concept.
    by_source: dict[ConceptId, list[dict]] = {}
    # Logical consumers should not inflate scores/details for repeated authored
    # occurrences.  ``graph.edges`` remains the occurrence-level view.
    for edge in graph.logical_edges():
        src = edge.source
        tgt = edge.target
        if source_cid is not None and src != source_cid:
            continue
        if target_cid is not None and tgt != target_cid:
            continue
        # Determine edge type and via (typed-relation vs markdown)
        edge_label = edge.label or ""
        via = "relations" if edge.origin == "relation" else "markdown"
        edge_type = edge_label if via == "relations" else ""

        # P2-1 (spec §4.3 "unioned" ambiguity, resolved toward "filter the
        # union"): when --relation TYPE is given, filter the UNION by edge
        # type. Markdown edges have an empty ``type`` so they drop out
        # naturally; typed-relation edges whose type does not match also
        # drop out. When NO --relation is given, ALL edges (frontmatter
        # relations ∪ markdown links) are kept.
        if rel_lc is not None and edge_type.lower() != rel_lc:
            continue

        # Apply type/tag filters on the source concept
        concept = content_index.by_id.get(src)
        if concept is None:
            continue
        if type_filter is not None and concept.type != type_filter:
            continue
        if tag_lc is not None and tag_lc not in {t.lower() for t in concept.tags}:
            continue
        by_source.setdefault(src, []).append({
            "type": edge_type,
            "target": concept_id_to_str(tgt),
            "via": via,
        })

    scored: list[tuple[float, ConceptId]] = [
        (float(len(edges)), src) for src, edges in by_source.items()
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))

    results: list[SearchResult] = []
    for score, src in scored[:limit]:
        concept = content_index.by_id[src]
        edges = by_source[src]
        # Build readable snippets from matched edges
        edge_snippets = [
            f"{e['type']} → {e['target']}" if e["type"] else f"→ {e['target']}"
            for e in edges
        ]
        results.append(
            SearchResult(
                concept_id=src,
                title=concept.title,
                score=score,
                # P2-5 (iter-3): relation snippets ARE the result data (the
                # matched edges). The full edge set also lives in
                # detail["edges"]; do not cap the readable rendering here —
                # capping silently hid most of a concept's edges in text
                # mode. The CLI text mode now shows all of them.
                snippets=edge_snippets,
                source_backend="relation",
                detail={"edges": edges},
            )
        )
    return results


__all__ = [
    "SearchMode",
    "SearchResult",
    "SearchBackend",
    "LexicalBackend",
    "SemanticLiteBackend",
    "HybridBackend",
    "DEFAULT_FIELD_WEIGHTS",
    "search_bundle",
    "mark_hybrid_active",
    "clear_search_cache",
    "tokenize",
]
