"""Bounded lexical + optional multilingual embedding retrieval over authorized snapshots."""
from .. import db
from ..text import tokens


def score(query, text):
    q, t = set(tokens(query)), set(tokens(text))
    return len(q & t) / max(1, len(q))


def select(document_ids, question, limit):
    rows = []
    by_doc = {}
    for did in document_ids:
        chunks = db.q("SELECT * FROM document_chunks WHERE document_id=? ORDER BY ordinal", (did,))
        by_doc[did] = chunks
        rows += chunks
    ranked = rows
    retrieval = "round_robin"
    if question:
        retrieval = "lexical"
        ranked = sorted(rows, key=lambda c: score(question, c["text"]), reverse=True)
        ranked = [c for c in ranked if score(question, c["text"]) > 0]
        # Embeddings are an independently configured capability, never inferred from chat support.
        from . import providers
        if providers.embedding_configured():
            ranked = providers.hybrid_rank(question, rows)
            retrieval = "hybrid_embedding_lexical"
    else:
        ranked = []
        for i in range(max((len(c) for c in by_doc.values()), default=0)):
            ranked.extend(c[i] for c in by_doc.values() if i < len(c))
    chosen = ranked[:limit]
    def covered(parts):
        total = 0
        for did in document_ids:
            until = 0
            for c in sorted((c for c in parts if c["document_id"] == did), key=lambda c: c["start_char"]):
                total += max(0, c["end_char"] - max(until, c["start_char"]))
                until = max(until, c["end_char"])
        return total
    return chosen, {"available_chunks": len(rows), "selected_chunks": len(chosen),
                    "available_characters": covered(rows),
                    "selected_characters": covered(chosen),
                    "complete_coverage": len(chosen) == len(rows), "retrieval": retrieval}


def candidates(claim, chunks, limit=2):
    ranked = sorted((c for c in chunks if c["document_id"] != claim["document_id"]),
                    key=lambda c: score(claim["text"], c["text"]), reverse=True)
    # Cross-language candidates may not overlap lexically; the bounded set is still checked by the model.
    return ranked[:limit]
