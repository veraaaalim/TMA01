# app.py
from flask import Flask, render_template, request, abort, url_for, redirect
from pymongo import MongoClient, errors
from bson import ObjectId
import os, sys

# --- local modules ---
from books import all_books                  # your provided seed data (list[dict])
from models.book import Book                 # the dataclass with seed_if_empty()

# ---------------- Flask app ----------------
app = Flask(__name__)

# --------------- MongoDB -------------------
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
try:
    client.admin.command("ping")
    print(f"[MongoDB] Connected → {MONGO_URI}")
except errors.ServerSelectionTimeoutError as e:
    print(f"[Mongo ERROR] Cannot connect → {e}", file=sys.stderr)

db = client["sg_library"]
books_coll = db["books"]

# Seed once if empty (and create indexes)
try:
    Book.seed_if_empty(books_coll, all_books)
except Exception as e:
    print(f"[Seed WARNING] {e}", file=sys.stderr)

# ------------- Helpers / constants ----------
CATEGORIES = ["All", "Children", "Teens", "Adult"]

def first_last(paras):
    parts = [p.strip() for p in (paras or []) if p and p.strip()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{parts[0]}\n\n{parts[-1]}"

# ---------------- Routes --------------------
@app.route("/")
def home():
    # open on Book Titles
    return redirect(url_for("titles_page"))

@app.route("/titles")
def titles_page():
    selected = request.args.get("category", "All")
    q = (request.args.get("q") or "").strip()

    # base filter (category)
    base_filter = {}
    if selected and selected != "All":
        base_filter["category"] = selected

    # projection
    fields = {
        "title": 1, "authors": 1, "url": 1,
        "category": 1, "genres": 1, "pages": 1, "description": 1
    }

    # search flow: text index → regex fallback → plain list
    docs = []
    if q:
        text_filter = dict(base_filter, **{"$text": {"$search": q}})
        docs = list(books_coll.find(text_filter, fields).sort("title", 1))

        if not docs:
            rx = {"$regex": q, "$options": "i"}
            rx_filter = {
                **base_filter,
                "$or": [
                    {"title": rx},
                    {"authors": rx},
                    {"genres": rx},
                    {"description": rx},
                ],
            }
            docs = list(books_coll.find(rx_filter, fields).sort("title", 1))
    if not docs:
        docs = list(books_coll.find(base_filter, fields).sort("title", 1))

    # transform for template cards
    cards = [{
        "_id": str(d["_id"]),
        "title": d.get("title", ""),
        "authors": ", ".join(d.get("authors", [])),
        "img": d.get("url", ""),
        "category_line": f"{d.get('category', '')}, " + ", ".join(d.get("genres", [])),
        "pages": d.get("pages", 0),
        "short_desc": first_last(d.get("description", [])),
    } for d in docs]

    return render_template(
        "titles.html",
        total=len(cards),
        cards=cards,
        categories=CATEGORIES,
        selected=selected,
        q=q,
    )

@app.route("/book/<key>")
def book_details(key):
    """
    Backward-compatible: accepts either a Mongo _id (ObjectId string)
    or a book title (exact match).
    """
    doc = None
    # try as ObjectId
    try:
        doc = books_coll.find_one({"_id": ObjectId(key)})
    except Exception:
        doc = None

    # fallback: by title
    if not doc:
        doc = books_coll.find_one({"title": key})

    if not doc:
        abort(404)

    doc["_id"] = str(doc["_id"])
    return render_template("book_details.html", book=doc)

# ----------------- Main ---------------------
if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    app.run(host=host, port=port, debug=True)
