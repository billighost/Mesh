import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import uuid

MESH_DIR = Path.home() / ".mesh"


class MeshStore:
    """
    Persistence layer for Mesh v0.2.

    ChromaDB: vector embeddings + similarity search
    SQLite tables:
      - memories: all stored memories with metadata + decay info
      - queries: every recall() call logged for pattern analysis
      - contradictions: conflict records from contradiction detection
    """

    def __init__(self, namespace: str = "default"):
        self.namespace = namespace
        MESH_DIR.mkdir(parents=True, exist_ok=True)

        import chromadb
        self.chroma_client = chromadb.PersistentClient(
            path=str(MESH_DIR / "chroma")
        )
        self.collection = self.chroma_client.get_or_create_collection(
            name=f"mesh_{namespace}",
            metadata={"hnsw:space": "cosine"}
        )

        self.db_path = MESH_DIR / f"mesh_{namespace}.db"
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)

        # Main memories table — extended with decay columns
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id              TEXT PRIMARY KEY,
                namespace       TEXT NOT NULL,
                content         TEXT NOT NULL,
                confidence      REAL NOT NULL DEFAULT 1.0,
                memory_type     TEXT NOT NULL DEFAULT 'fact',
                source_agent    TEXT NOT NULL DEFAULT 'unknown',
                tags            TEXT NOT NULL DEFAULT '[]',
                created_at      TEXT NOT NULL,
                accessed_at     TEXT NOT NULL,
                access_count    INTEGER NOT NULL DEFAULT 0,
                ttl_days        REAL,
                decay_start_at  TEXT
            )
        """)

        # Add decay columns to existing tables if upgrading from v0.1
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN ttl_days REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN decay_start_at TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN local_only INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Queries log — every recall() call is recorded here
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queries (
                id              TEXT PRIMARY KEY,
                namespace       TEXT NOT NULL,
                query_text      TEXT NOT NULL,
                agent_id        TEXT NOT NULL DEFAULT 'unknown',
                best_similarity REAL,
                result_count    INTEGER NOT NULL DEFAULT 0,
                timestamp       TEXT NOT NULL
            )
        """)

        # Contradictions table — conflict records
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contradictions (
                id                  TEXT PRIMARY KEY,
                namespace           TEXT NOT NULL,
                new_memory_id       TEXT NOT NULL,
                new_content         TEXT NOT NULL,
                existing_memory_id  TEXT NOT NULL,
                existing_content    TEXT NOT NULL,
                similarity          REAL NOT NULL,
                detected_at         TEXT NOT NULL,
                resolved            INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_namespace ON memories (namespace)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memories (memory_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_namespace ON queries (namespace)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_timestamp ON queries (timestamp)")

        conn.commit()
        conn.close()

    # ── Core memory operations ──────────────────────────────────────────

    def upsert(self, memory_id: str, content: str,
               embedding: list[float], metadata: dict, local_only: bool = False) -> None:
        """Store or update a memory in both ChromaDB and SQLite."""
        metadata["local_only"] = 1 if local_only else 0
        chroma_meta = {
            k: v for k, v in metadata.items()
            if isinstance(v, (str, int, float, bool))
        }

        self.collection.upsert(
            ids=[memory_id],
            embeddings=[embedding],
            documents=[content],
            metadatas=[chroma_meta]
        )

        now = datetime.utcnow().isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO memories
            (id, namespace, content, confidence, memory_type,
             source_agent, tags, created_at, accessed_at, access_count,
             ttl_days, decay_start_at, local_only)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """, (
            memory_id,
            self.namespace,
            content,
            metadata.get("confidence", 1.0),
            metadata.get("memory_type", "fact"),
            metadata.get("source_agent", "unknown"),
            json.dumps(metadata.get("tags", [])),
            metadata.get("created_at", now),
            now,
            metadata.get("ttl_days"),
            metadata.get("decay_start_at"),
            1 if local_only else 0,
        ))
        conn.commit()
        conn.close()

    def query(self, embedding: list[float], n: int = 5) -> dict:
        """Semantic similarity search."""
        count = self.collection.count()
        if count == 0:
            return {
                "documents": [[]],
                "distances": [[]],
                "metadatas": [[]],
                "ids": [[]]
            }
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=min(n, count)
        )
        return results

    def delete(self, memory_id: str) -> None:
        """Remove a memory from both stores."""
        self.collection.delete(ids=[memory_id])
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        conn.close()

    def increment_access(self, memory_id: str) -> None:
        now = datetime.utcnow().isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            UPDATE memories
            SET access_count = access_count + 1, accessed_at = ?
            WHERE id = ?
        """, (now, memory_id))
        conn.commit()
        conn.close()

    def count(self) -> int:
        return self.collection.count()

    def list_all(self, limit: int = 100) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM memories
            WHERE namespace = ?
            ORDER BY accessed_at DESC
            LIMIT ?
        """, (self.namespace, limit)).fetchall()
        conn.close()
        
        res = []
        for r in rows:
            d = dict(r)
            d["local_only"] = bool(d.get("local_only", 0))
            res.append(d)
        return res

    def get_exportable_memories(self) -> list[dict]:
        """Return all memories where local_only is False."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM memories 
            WHERE namespace = ? AND local_only = 0 
            ORDER BY created_at DESC
        """, (self.namespace,))
        rows = cursor.fetchall()
        conn.close()
        
        res = []
        for r in rows:
            d = dict(r)
            d["local_only"] = False
            res.append(d)
        return res

    def clear_namespace(self) -> None:
        all_items = self.collection.get()
        if all_items["ids"]:
            self.collection.delete(ids=all_items["ids"])
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM memories WHERE namespace = ?", (self.namespace,))
        conn.execute("DELETE FROM queries WHERE namespace = ?", (self.namespace,))
        conn.commit()
        conn.close()

    # ── Query logging (for pattern detection) ──────────────────────────

    def log_query(self, query_text: str, agent_id: str,
                  best_similarity: Optional[float],
                  result_count: int) -> None:
        """Log every recall() call for pattern analysis."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO queries
            (id, namespace, query_text, agent_id, best_similarity,
             result_count, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid4()),
            self.namespace,
            query_text,
            agent_id,
            best_similarity,
            result_count,
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        conn.close()

    # ── Contradiction tracking ──────────────────────────────────────────

    def log_contradiction(self, new_memory_id: str, new_content: str,
                          existing_memory_id: str, existing_content: str,
                          similarity: float) -> str:
        """Record a detected contradiction. Returns contradiction id."""
        conflict_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO contradictions
            (id, namespace, new_memory_id, new_content,
             existing_memory_id, existing_content, similarity, detected_at, resolved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            conflict_id,
            self.namespace,
            new_memory_id,
            new_content,
            existing_memory_id,
            existing_content,
            similarity,
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        conn.close()
        return conflict_id

    def get_contradictions(self, resolved: bool = False) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM contradictions
            WHERE namespace = ? AND resolved = ?
            ORDER BY detected_at DESC
        """, (self.namespace, 1 if resolved else 0)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def resolve_contradiction(self, contradiction_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE contradictions SET resolved = 1 WHERE id = ?",
            (contradiction_id,)
        )
        conn.commit()
        conn.close()

    # ── Pattern analysis ────────────────────────────────────────────────

    def get_patterns(self) -> dict:
        """
        Analyse query and memory data to surface:
        - top_topics: most queried subjects
        - memory_gaps: topics queried often but with low similarity results
        - unused_memories: memories never recalled after 7+ days
        - stale_memories: memories whose effective confidence is below 0.3
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        now = datetime.utcnow()

        # Top topics — most queried in last 30 days
        cutoff = (now - timedelta(days=30)).isoformat()
        top_queries = conn.execute("""
            SELECT query_text, COUNT(*) as query_count,
                   AVG(best_similarity) as avg_similarity
            FROM queries
            WHERE namespace = ? AND timestamp > ?
            GROUP BY query_text
            ORDER BY query_count DESC
            LIMIT 10
        """, (self.namespace, cutoff)).fetchall()

        # Memory gaps — queries with consistently low similarity
        # (asked 2+ times, average similarity below 0.4)
        gaps = conn.execute("""
            SELECT query_text, COUNT(*) as query_count,
                   AVG(best_similarity) as avg_similarity
            FROM queries
            WHERE namespace = ? AND timestamp > ?
              AND best_similarity IS NOT NULL
            GROUP BY query_text
            HAVING COUNT(*) >= 2 AND AVG(best_similarity) < 0.4
            ORDER BY query_count DESC
            LIMIT 10
        """, (self.namespace, cutoff)).fetchall()

        # Unused memories — never recalled, created 7+ days ago
        week_ago = (now - timedelta(days=7)).isoformat()
        unused = conn.execute("""
            SELECT id, content, created_at, memory_type, source_agent
            FROM memories
            WHERE namespace = ? AND access_count = 0
              AND created_at < ?
            ORDER BY created_at ASC
            LIMIT 10
        """, (self.namespace, week_ago)).fetchall()

        # Stale memories — have ttl_days set and are decaying badly
        all_memories = conn.execute("""
            SELECT id, content, confidence, ttl_days, decay_start_at,
                   created_at, memory_type, source_agent
            FROM memories
            WHERE namespace = ? AND ttl_days IS NOT NULL
        """, (self.namespace,)).fetchall()

        stale = []
        for m in all_memories:
            m = dict(m)
            decay_start = m.get("decay_start_at") or m.get("created_at")
            if decay_start and m["ttl_days"]:
                try:
                    start_dt = datetime.fromisoformat(decay_start)
                    days_elapsed = (now - start_dt).days
                    decay_factor = max(0.0, 1.0 - (days_elapsed / m["ttl_days"]))
                    effective_conf = m["confidence"] * decay_factor
                    if effective_conf < 0.3:
                        m["effective_confidence"] = round(effective_conf, 3)
                        m["days_elapsed"] = days_elapsed
                        stale.append(m)
                except Exception:
                    pass

        conn.close()

        return {
            "top_topics": [dict(r) for r in top_queries],
            "memory_gaps": [dict(r) for r in gaps],
            "unused_memories": [dict(r) for r in unused],
            "stale_memories": stale,
        }
