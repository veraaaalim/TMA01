from dataclasses import dataclass, field
from typing import List, Dict, Any

@dataclass
class Book:
    title: str
    authors: List[str] = field(default_factory=list)
    category: str = ""
    genres: List[str] = field(default_factory=list)
    url: str = ""
    description: List[str] = field(default_factory=list)
    pages: int = 0
    copies: int = 0
    available: int = 0

    @staticmethod
    def from_source(b: Dict[str, Any]) -> "Book":
        return Book(
            title=(b.get("title") or "").strip(),
            authors=b.get("authors", []) or [],
            category=(b.get("category") or "").strip(),
            genres=b.get("genres", []) or [],
            url=(b.get("url") or "").strip(),
            description=b.get("description", []) or [],
            pages=int(b.get("pages") or 0),
            copies=int(b.get("copies") or 0),
            available=int(b.get("available") or 0),
        )

    def to_doc(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "authors": self.authors,
            "category": self.category,
            "genres": self.genres,
            "url": self.url,
            "description": self.description,
            "pages": self.pages,
            "copies": self.copies,
            "available": self.available,
        }

    @classmethod
    def seed_if_empty(cls, coll, source_list: List[Dict[str, Any]]) -> None:
        """Insert books from source_list if collection is empty; also builds indexes."""
        if coll.count_documents({}) == 0:
            docs = [cls.from_source(b).to_doc() for b in source_list]
            if docs:
                coll.insert_many(docs)
                coll.create_index("title")
                coll.create_index("category")
                coll.create_index([
                    ("title", "text"),
                    ("authors", "text"),
                    ("genres", "text"),
                    ("description", "text"),
                ])
