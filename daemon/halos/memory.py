"""Memory manager for HalOS."""

import re
from pathlib import Path
from .db import Database


class MemoryManager:
    def __init__(self, memory_path: str, db: Database):
        self.memory_path = memory_path
        self.db = db
        self._flat_memory: str = ""

    async def load(self):
        """Load flat memory file from disk."""
        path = Path(self.memory_path)
        if path.exists():
            self._flat_memory = path.read_text()
        else:
            self._flat_memory = ""

    def get_flat_memory(self) -> str:
        return self._flat_memory

    async def get_relevant_context(self, message: str) -> str:
        """Build a context string from flat memory + any relevant structured memory."""
        parts = []

        if self._flat_memory:
            parts.append(self._flat_memory)

        # Search structured memory for anything relevant
        keywords = [w for w in message.lower().split() if len(w) > 3]
        seen = set()
        for kw in keywords[:5]:
            results = await self.db.search_memory(kw)
            for r in results:
                mem_key = (r["category"], r["key"])
                if mem_key not in seen:
                    seen.add(mem_key)
                    parts.append(f"[{r['category']}] {r['key']}: {r['value']}")

        return "\n\n".join(parts)

    async def remember(self, key: str, value: str, category: str = "fact", source: str = "user") -> str:
        """Add something to structured memory."""
        await self.db.add_memory(category, key, value, source)
        return f"Remembered [{category}] {key}: {value}"

    async def forget(self, key: str) -> str:
        """Remove something from structured memory by key. Searches all categories."""
        all_mem = await self.db.get_memory()
        deleted = False
        for m in all_mem:
            if key.lower() in m["key"].lower():
                await self.db.delete_memory(m["category"], m["key"])
                deleted = True
        if deleted:
            return f"Forgot everything matching '{key}'"
        return f"Nothing found matching '{key}'"

    async def sync_to_file(self, memory_path: str, source: str = "") -> bool:
        """Sync structured memory (SQLite) back to a memory.md file.

        Replaces the '## Learned' section with current SQLite entries.
        Creates the section if it doesn't exist. Returns True if file was updated.
        """
        path = Path(memory_path)
        if not path.exists():
            return False

        entries = await self.db.get_memory()
        if source:
            entries = [e for e in entries if e.get("source", "") == source]

        if not entries:
            return False

        learned_lines = [f"## Learned\n"]
        for e in entries:
            learned_lines.append(f"- [{e['category']}] {e['key']}: {e['value']}")
        learned_block = "\n".join(learned_lines)

        text = path.read_text()
        if "## Learned" in text:
            # Replace existing Learned section
            text = re.sub(
                r"## Learned\n.*?(?=\n## |\Z)",
                learned_block + "\n",
                text,
                flags=re.DOTALL,
            )
        else:
            text = text.rstrip() + "\n\n" + learned_block + "\n"

        path.write_text(text)
        return True

    async def list_memory(self, query: str = None) -> str:
        """List memory entries, optionally filtered by query."""
        if query:
            entries = await self.db.search_memory(query)
        else:
            entries = await self.db.get_memory()

        if not entries:
            return "Memory is empty." if not query else f"Nothing found matching '{query}'"

        lines = []
        current_cat = None
        for e in entries:
            if e["category"] != current_cat:
                current_cat = e["category"]
                lines.append(f"\n*{current_cat.upper()}*")
            lines.append(f"  {e['key']}: {e['value']}")

        return "\n".join(lines).strip()
