"""Read on ingestion/deep health only; /health's normal path stays in memory."""
_snapshot: dict | None = None

async def refresh(pool) -> dict:
    global _snapshot
    row = await pool.fetchrow("""
        SELECT count(*) FILTER (WHERE generation_status = 'pending') AS pending,
               count(*) FILTER (WHERE generation_status = 'unavailable') AS unavailable,
               max(generation_attempted_at) FILTER (WHERE generation_status = 'complete') AS last_completed
          FROM articles WHERE from_search = false
            AND created_at >= NOW() - INTERVAL '30 days'
            AND (generation_status <> 'complete' OR generation_attempted_at IS NOT NULL)
    """)
    _snapshot = {"pending": row["pending"], "unavailable": row["unavailable"],
                 "last_completed": row["last_completed"].isoformat() if row["last_completed"] else None}
    return _snapshot

def snapshot():
    return _snapshot
