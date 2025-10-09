from flask import (
    Flask, render_template, request, abort,
    url_for, redirect
)
from pymongo import MongoClient, errors
from bson import ObjectId
import os, sys

# -------- Flask app --------
app = Flask(__name__)

# -------- MongoDB connection --------
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)

try:
    client.admin.command("ping")
    print(f"[MongoDB] Connected to {MONGO_URI}")
except errors.ServerSelectionTimeoutError as e:
    print(f"[Mongo ERROR] Cannot connect → {e}", file=sys.stderr)

db = client["sg_library"]
books_coll = db["books"]

# ---- Seed Books (only once if empty) ----
from books import all_books  # source data for seeding

def seed_books_if_empty(coll, all_books):
    """Seeds the books collection if it is empty."""
    if coll.estimated_document_count() == 0:
        docs = []
        for b in all_books:
            docs.append({
                "title": b.get("title", "").strip(),
                "authors": b.get("authors", []),
                "category": b.get("category", "").strip(),
                "genres": b.get("genres", []),
                "url": b.get("url", "").strip(),
                "description": b.get("description", []),
                "pages": int(b.get("pages", 0)) if b.get("pages") else 0,
                "copies": int(b.get("copies", 0)) if b.get("copies") else 0,
                "available": int(b.get("available", 0))
            })

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
            print(f"[MongoDB] ✅ Seeded {len(docs)} books and created indexes")

try:
    seed_books_if_empty(books_coll, all_books)
except Exception as e:
    print(f"[Seed WARNING] {e}", file=sys.stderr)

# -------- Constants/helpers --------
CATEGORIES = ["All", "Children", "Teens", "Adult"]

def first_last(paras):
    parts = [p.strip() for p in (paras or []) if p and p.strip()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{parts[0]}\n\n{parts[-1]}"

# -------- Routes --------
@app.route("/")
def home():
    return redirect(url_for("titles_page"))

@app.route("/titles")
def titles_page():
    selected = request.args.get("category", "All")
    q = request.args.get("q", "").strip()

    base_filter = {}
    if selected and selected != "All":
        base_filter["category"] = selected

    fields = {
        "title": 1, "authors": 1, "url": 1,
        "category": 1, "genres": 1, "pages": 1, "description": 1
    }

    if q:
        # Text search first
        text_filter = dict(base_filter)
        text_filter["$text"] = {"$search": q}
        docs = list(books_coll.find(text_filter, fields).sort("title", 1))

        # Fallback to regex
        if not docs:
            rx = {"$regex": q, "$options": "i"}
            rx_filter = {
                **base_filter,
                "$or": [
                    {"title": rx},
                    {"authors": rx},
                    {"genres": rx},
                    {"description": rx}
                ]
            }
            docs = list(books_coll.find(rx_filter, fields).sort("title", 1))
    else:
        docs = list(books_coll.find(base_filter, fields).sort("title", 1))

    cards = [{
        "_id": str(d["_id"]),
        "title": d.get("title", ""),
        "authors": ", ".join(d.get("authors", [])),
        "img": d.get("url", ""),
        "category_line": f"{d.get('category', '')}, " + ", ".join(d.get("genres", [])),
        "pages": d.get("pages", 0),
        "short_desc": first_last(d.get("description", []))
    } for d in docs]

    return render_template(
        "titles.html",
        total=len(cards),
        cards=cards,
        categories=CATEGORIES,
        selected=selected,
        q=q
    )

@app.route("/book/<id>")
def book_details(id):
    try:
        obj_id = ObjectId(id)
    except Exception:
        abort(404)
    d = books_coll.find_one({"_id": obj_id})
    if not d:
        abort(404)
    d["_id"] = str(d["_id"])
    return render_template("book_details.html", book=d)

# -------- Main --------
if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8080"))
    app.run(host=host, port=port, debug=True)
