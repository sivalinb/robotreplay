import re

from robotreplay.policy import input_policy


def retrieve(store, team, clip_ids, query, limit=6):
    words = list(dict.fromkeys(re.findall(r"\w+", query.lower(), flags=re.UNICODE)))[:20]
    marks = ",".join("?" for _ in clip_ids)
    rows = []
    if words and clip_ids:
        expression = " OR ".join('"' + word + '"' for word in words)
        rows = store.rows(
            f"""SELECT e.*, bm25(evidence_fts) AS rank FROM evidence_fts
              JOIN evidence e ON e.id=evidence_fts.id
              WHERE evidence_fts MATCH ? AND e.team=? AND e.clip_id IN ({marks})
              ORDER BY rank LIMIT ?""",
            (expression, team, *clip_ids, limit * 2),
        )
    strategy = "fts5"
    if not rows and clip_ids:
        strategy = "chronological_fallback"
        rows = store.rows(
            f"SELECT * FROM evidence WHERE team=? AND clip_id IN ({marks}) ORDER BY t LIMIT ?",
            (team, *clip_ids, limit * 2),
        )
    # Captions/logs/annotations remain data; suspicious entries never become tool instructions.
    safe = [r for r in rows if input_policy(r["text"]).allowed][:limit]
    return safe, strategy, len(rows) - len(safe)


def reciprocal_rank_fusion(lexical_ids, semantic_ids, limit=6):
    scores = {}
    for ranking in (lexical_ids, semantic_ids):
        for rank, evidence_id in enumerate(ranking, 1):
            scores[evidence_id] = scores.get(evidence_id, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]
