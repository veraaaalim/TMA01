# models/book.py
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from bson import ObjectId
from pymongo.collection import Collection

# ---- Schema (matches class diagram in Q2(b)(i)) ----
@dataclass
class BookDoc:
    genres: List[str]
    title: str
    category: str
    url: str
    description: List[str]
    authors: List[str]
    pages: int
    available: int   # store 1/0 as int
    copies: int

    @staticmethod
    def from_raw(raw: Dict[str, Any]) -> "BookDoc":
        # Handle list/int/boolean conversions safely
        to_list = lambda v: list(v) if isinstance(v, (list, tuple)) else ([v] if v else [])
        to_int  = lambda v, d=0: int(v) if v is not None and str(v).strip() != "" else d
        to01    = lambda v: 1 if (v in (True, 1, "1", "true", "True", "yes")) else 0

        return BookDoc(
            genres=to_list(raw.get("genres")),
            title=str(raw["title"]),
            category=str(raw.get("category", "")),
            url=str(raw.get("url", "")),
            description=to_list(raw.get("description")),
            authors=to_list(raw.get("authors")),
            pages=to_int(raw.get("pages")),
            available=to01(raw.get("available", 1)),
            copies=to_int(raw.get("copies", 1)),
        )

# ---- Data access object ----
class Book:
    def __init__(self, col: Collection):
        self.col = col
        # helpful indexes
        self.col.create_index([("title", "text")])
        self.col.create_index("category")

def seed_if_empty(coll, all_books):
    """Seed Mongo collection if empty, with indexes for search."""
    if coll.estimated_document_count() == 0:
        docs = []
        for b in all_books:
            # flatten + normalize each book dictionary
            doc = {
                "title": b.get("title", ""),
                "authors": b.get("authors", []),
                "category": b.get("category", ""),
                "genres": b.get("genres", []),
                "url": b.get("url", ""),
                "description": b.get("description", []),
                "pages": b.get("pages", 0),
                "copies": b.get("copies", 0),
                "available": 1 if b.get("available") else 0
            }
            docs.append(doc)

        if docs:
            coll.insert_many(docs)
            coll.create_index("title")
            coll.create_index("category")
            coll.create_index([
                ("title", "text"),
                ("authors", "text"),
                ("genres", "text"),
                ("description", "text")
            ])



    def list(self, category: Optional[str] = None):
        q = {} if not category or category == "All" else {"category": category}
        return list(self.col.find(q).sort("title", 1))

    def get(self, _id: str):
        return self.col.find_one({"_id": ObjectId(_id)})

    def search_title(self, text: str, category: Optional[str] = None):
        q = {"$text": {"$search": text}}
        if category and category != "All":
            q["category"] = category
        return list(self.col.find(q).sort("title", 1))
