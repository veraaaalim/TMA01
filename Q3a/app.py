from flask import (
    Flask, render_template, request, abort,
    url_for, redirect, flash
)
from pymongo import MongoClient, errors
from bson import ObjectId
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os, sys

# -------- Flask app --------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me")

# -------- MongoDB --------
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
try:
    client.admin.command("ping")
    print(f"[MongoDB] Connected to {MONGO_URI}")
except errors.ServerSelectionTimeoutError as e:
    print(f"[Mongo ERROR] Cannot connect → {e}", file=sys.stderr)

db = client["sg_library"]
books_coll = db["books"]
users_coll = db["users"]

# ---- Seed Books from provided list on first run ----
from books import all_books  # source data for seeding

def seed_books_if_empty(coll, all_books):
    if coll.estimated_document_count() == 0:
        docs = []
        for b in all_books:
            docs.append({
                "title": b.get("title", ""),
                "authors": b.get("authors", []),
                "category": b.get("category", ""),
                "genres": b.get("genres", []),
                "url": b.get("url", ""),
                "description": b.get("description", []),
                "pages": b.get("pages", 0),
                "copies": b.get("copies", 0),
                "available": 1 if b.get("available") else 0
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
            print("[MongoDB] Seeded books and created indexes")

try:
    seed_books_if_empty(books_coll, all_books)
except Exception as e:
    print(f"[Seed WARNING] {e}", file=sys.stderr)

# ---- Seed default users (required by the question) ----
def seed_users_if_empty():
    if users_coll.estimated_document_count() == 0:
        users_coll.insert_many([
            {
                "email": "admin@lib.sg",
                "password": generate_password_hash("12345"),
                "name": "Admin",
                "is_admin": True
            },
            {
                "email": "poh@lib.sg",
                "password": generate_password_hash("12345"),
                "name": "Peter Oh",
                "is_admin": False
            }
        ])
        users_coll.create_index("email", unique=True)
        print("[MongoDB] Seeded default users")

try:
    seed_users_if_empty()
except Exception as e:
    print(f"[User Seed WARNING] {e}", file=sys.stderr)

# -------- Auth (Flask-Login) --------
login_manager = LoginManager(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(self, doc):
        self.id = str(doc["_id"])
        self.email = doc["email"]
        self.name = doc.get("name", "")
        self.is_admin = bool(doc.get("is_admin", False))

@login_manager.user_loader
def load_user(user_id):
    doc = users_coll.find_one({"_id": ObjectId(user_id)})
    return User(doc) if doc else None

# -------- App constants/helpers --------
CATEGORIES = ["All", "Children", "Teens", "Adult"]

GENRES = [
    "Animals","Business","Comics","Communication","Dark Academia","Emotion","Fantasy",
    "Fiction","Friendship","Graphic Novels","Grief","Historical Fiction","Indigenous",
    "Inspirational","Magic","Mental Health","Nonfiction","Personal Development",
    "Philosophy","Picture Books","Poetry","Productivity","Psychology","Romance",
    "School","Self Help"
]

def first_last(paras):
    parts = [p.strip() for p in paras if p and p.strip()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{parts[0]}\n\n{parts[-1]}"

def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return view(*args, **kwargs)
    return wrapped

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

    fields = {"title": 1, "authors": 1, "url": 1, "category": 1, "genres": 1, "pages": 1, "description": 1}

    if q:
        # Try text search first
        text_filter = dict(base_filter)
        text_filter["$text"] = {"$search": q}
        docs = list(books_coll.find(text_filter, fields).sort("title", 1))

        # Fallback to regex if nothing found
        if not docs:
            rx = {"$regex": q, "$options": "i"}
            rx_filter = {
                **base_filter,
                "$or": [{"title": rx}, {"authors": rx}, {"genres": rx}, {"description": rx}]
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

# -------- Auth pages --------
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("titles_page"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        name = request.form.get("name", "").strip()

        if not email or not password or not name:
            flash("Please fill in all fields.", "error")
            return redirect(url_for("register"))

        if users_coll.find_one({"email": email}):
            flash("Email already registered.", "error")
            return redirect(url_for("register"))

        users_coll.insert_one({
            "email": email,
            "password": generate_password_hash(password),
            "name": name,
            "is_admin": email == "admin@lib.sg"
        })
        flash("Registered successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("titles_page"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        user_doc = users_coll.find_one({"email": email})
        if user_doc and check_password_hash(user_doc["password"], password):
            login_user(User(user_doc))
            flash("Login successful.", "success")
            return redirect(url_for("titles_page"))
        flash("Invalid email or password.", "error")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("titles_page"))

# -------- New Book (admin only) --------
@app.route("/books/new", methods=["GET", "POST"])
@admin_required
def new_book():
    if not current_user.is_admin:
        flash("Only admin users can add new books.", "error")
        return redirect(url_for("titles_page"))
    if request.method == "POST":
        # ---- read fields ----
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        url_ = request.form.get("url", "").strip()
        description = request.form.get("description", "").splitlines()
        genres = request.form.getlist("genres")

        # authors (up to 5) + illustrator flags
        a_names, illustrators = [], []
        for i in range(1, 6):
            name = request.form.get(f"author{i}", "").strip()
            if name:
                a_names.append(name)
                if request.form.get(f"illus{i}") == "on":
                    illustrators.append(name)

        # numeric
        def as_int(v, default=0):
            try:
                return int(v)
            except Exception:
                return default
        pages = as_int(request.form.get("pages", "0"))
        copies = as_int(request.form.get("copies", "1"))

        # minimal validation
        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("new_book"))
        if category not in [c for c in CATEGORIES if c != "All"]:
            flash("Please choose a valid category.", "error")
            return redirect(url_for("new_book"))

        doc = {
            "title": title,
            "authors": a_names,
            "illustrators": illustrators,   # optional field
            "category": category,
            "genres": genres,
            "url": url_,
            "description": description,
            "pages": pages,
            "copies": copies
        }
        try:
            books_coll.insert_one(doc)
            flash(f"“{title}” added successfully.", "success")
        except Exception as e:
            flash(f"Failed to add book: {e}", "error")

        # stay on same page
        return redirect(url_for("new_book"))

    # GET
    return render_template(
        "new_book.html",
        categories=[c for c in CATEGORIES if c != "All"],
        genres=GENRES
    )

# -------- Main --------
if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8080"))
    app.run(host=host, port=port, debug=True)
