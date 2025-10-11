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
from datetime import datetime, timezone
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
books_coll  = db["books"]
users_coll  = db["users"]
loans_coll  = db["loans"]

# ---- Seed Books from provided list on first run ----
from books import all_books  # source data for seeding

def seed_books_if_empty(coll, all_books):
    if coll.estimated_document_count() == 0:
        docs = []
        for b in all_books:
            copies = int(b.get("copies", 0))
            available = b.get("available", copies)
            try:
                available = int(available)
            except Exception:
                available = copies
            docs.append({
                "title": b.get("title", ""),
                "authors": b.get("authors", []),
                "category": b.get("category", ""),
                "genres": b.get("genres", []),
                "url": b.get("url", ""),
                "description": b.get("description", []),
                "pages": b.get("pages", 0),
                "copies": copies,
                "available": max(0, available)
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

# ---- Seed default users ----
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

try:
    loans_coll.create_index([("member", 1)])
    loans_coll.create_index([("book", 1)])
    loans_coll.create_index([("borrowDate", -1)])
    loans_coll.create_index([("member", 1), ("book", 1), ("returnDate", 1)])
except Exception as e:
    print(f"[Loan Index WARNING] {e}", file=sys.stderr)

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
    try:
        doc = users_coll.find_one({"_id": ObjectId(user_id)})
    except Exception:
        doc = None
    return User(doc) if doc else None

# -------- Book domain wrapper --------
class Book:
    """Thin domain wrapper for a book document with borrow/return behavior."""
    def __init__(self, coll, doc):
        self._coll = coll
        self.doc = doc
        self.id = doc["_id"] if isinstance(doc["_id"], ObjectId) else ObjectId(doc["_id"])

    @property
    def copies(self) -> int:
        try:
            return int(self.doc.get("copies", 0))
        except Exception:
            return 0

    @property
    def available(self) -> int:
        try:
            return int(self.doc.get("available", self.copies))
        except Exception:
            return self.copies

    @classmethod
    def get(cls, coll, book_id: str | ObjectId):
        try:
            oid = ObjectId(book_id)
        except Exception:
            return None
        doc = coll.find_one({"_id": oid})
        return cls(coll, doc) if doc else None

    def _reload(self):
        fresh = self._coll.find_one({"_id": self.id})
        if fresh:
            self.doc = fresh

    def borrow(self) -> tuple[bool, str]:
        """Decrease available by 1 if > 0. Atomic and bounded."""
        if self.available <= 0:
            return False, "No copies available to loan."
        res = self._coll.update_one(
            {"_id": self.id, "available": {"$gt": 0}},
            {"$inc": {"available": -1}}
        )
        if res.modified_count == 1:
            self._reload()
            return True, "Loan created."
        return False, "Loan failed due to a concurrent update. Please try again."

    def return_one(self) -> tuple[bool, str]:
        """Increase available by 1 only if at least one copy was borrowed."""
        if self.available >= self.copies:
            return False, "All copies are already in stock."
        res = self._coll.update_one(
            {"_id": self.id, "available": {"$lt": self.copies}},
            {"$inc": {"available": +1}}
        )
        if res.modified_count == 1:
            self._reload()
            return True, "Book returned."
        return False, "Return failed due to a concurrent update. Please try again."

# -------- Loan class --------
class Loan:
    """
    Loan document model (diagram-compliant):
      _id: ObjectId
      member: ObjectId  (ref -> users._id)
      book: ObjectId    (ref -> books._id)
      borrowDate: datetime (UTC)
      returnDate: datetime or absent
      renewCount: int
    """
    def __init__(self, coll, doc):
        self._coll = coll
        self.doc = doc
        self.id = doc["_id"] if isinstance(doc["_id"], ObjectId) else ObjectId(doc["_id"])

    # --- Retrieval ---
    @classmethod
    def get(cls, coll, loan_id: str | ObjectId):
        try:
            oid = ObjectId(loan_id)
        except Exception:
            return None
        d = coll.find_one({"_id": oid})
        return cls(coll, d) if d else None

    @classmethod
    def list_for_user(cls, coll, user_id: str | ObjectId):
        try:
            uid = ObjectId(user_id)
        except Exception:
            return []
        docs = list(coll.find({"member": uid}).sort("borrowDate", -1))
        return [cls(coll, d) for d in docs]

    @classmethod
    def has_open_loan(cls, coll, user_id: ObjectId, book_id: ObjectId) -> bool:
        return coll.count_documents({
            "member": user_id,
            "book": book_id,
            "returnDate": {"$exists": False}
        }) > 0

    # --- Create ---
    @classmethod
    def create(cls, loans_coll, books_coll, user_id: str | ObjectId,
               book_id: str | ObjectId, borrow_date: datetime | None = None) -> tuple[bool, str, "Loan|None"]:
        try:
            uid = ObjectId(user_id); bid = ObjectId(book_id)
        except Exception:
            return False, "Invalid user or book id.", None

        if cls.has_open_loan(loans_coll, uid, bid):
            return False, "You already have an unreturned loan for this title.", None

        book = Book.get(books_coll, bid)
        if not book:
            return False, "Book not found.", None

        ok, msg = book.borrow()
        if not ok:
            return False, msg, None

        loan_doc = {
            "member": uid,
            "book": bid,
            "borrowDate": (borrow_date or datetime.now(timezone.utc)),
            "renewCount": 0
        }
        try:
            ins = loans_coll.insert_one(loan_doc)
            loan_doc["_id"] = ins.inserted_id
            return True, "Loan created.", cls(loans_coll, loan_doc)
        except Exception as e:
            book.return_one()
            return False, f"Failed to create loan: {e}", None

    # --- Update: renew ---
    def renew(self) -> tuple[bool, str]:
        if self.doc.get("returnDate"):
            return False, "Cannot renew a returned loan."
        res = self._coll.update_one(
            {"_id": self.id, "returnDate": {"$exists": False}},
            {
                "$set": {"borrowDate": datetime.now(timezone.utc)},
                "$inc": {"renewCount": 1}
            }
        )
        if res.modified_count == 1:
            self.doc = self._coll.find_one({"_id": self.id})
            return True, "Loan renewed."
        return False, "Renew failed (maybe concurrent update)."

    # --- Update: return ---
    def mark_returned(self, books_coll) -> tuple[bool, str]:
        if self.doc.get("returnDate"):
            return False, "Loan already returned."

        now = datetime.now(timezone.utc)
        res = self._coll.update_one(
            {"_id": self.id, "returnDate": {"$exists": False}},
            {"$set": {"returnDate": now}}
        )
        if res.modified_count != 1:
            return False, "Return failed (maybe concurrent update)."

        book = Book.get(books_coll, self.doc["book"])
        if not book:
            return False, "Book not found to complete return."
        bok, bmsg = book.return_one()
        if not bok:
            return False, bmsg

        self.doc = self._coll.find_one({"_id": self.id})
        return True, "Book returned."

    # --- Delete ---
    def delete(self) -> tuple[bool, str]:
        if not self.doc.get("returnDate"):
            return False, "Only returned loans can be deleted."
        res = self._coll.delete_one({"_id": self.id})
        if res.deleted_count == 1:
            return True, "Loan deleted."
        return False, "Delete failed."

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

    fields = {
        "title": 1, "authors": 1, "url": 1, "category": 1, "genres": 1,
        "pages": 1, "description": 1,
        "copies": 1, "available": 1
    }

    if q:
        text_filter = dict(base_filter)
        text_filter["$text"] = {"$search": q}
        docs = list(books_coll.find(text_filter, fields).sort("title", 1))
        if not docs:
            rx = {"$regex": q, "$options": "i"}
            rx_filter = {
                **base_filter,
                "$or": [{"title": rx}, {"authors": rx}, {"genres": rx}, {"description": rx}]
            }
            docs = list(books_coll.find(rx_filter, fields).sort("title", 1))
    else:
        docs = list(books_coll.find(base_filter, fields).sort("title", 1))

    cards = []
    for d in docs:
        try:
            avail = int(d.get("available", d.get("copies", 0)))
        except Exception:
            avail = 0

        cards.append({
            "_id": str(d["_id"]),
            "title": d.get("title", ""),
            "authors": ", ".join(d.get("authors", [])),
            "img": d.get("url", ""),
            "category_line": f"{d.get('category', '')}, " + ", ".join(d.get("genres", [])),
            "pages": d.get("pages", 0),
            "short_desc": first_last(d.get("description", [])),
            "available": avail,
            "copies": d.get("copies", 0),
        })

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

# ---- Loan routes  ----
@app.route("/loans", methods=["GET"])
@login_required
def my_loans():
    loans = [L.doc for L in Loan.list_for_user(loans_coll, current_user.id)]
    view = []
    for doc in loans:
        b = books_coll.find_one({"_id": doc["book"]}, {"title":1, "url":1})
        view.append({
            "_id": str(doc["_id"]),
            "title": (b or {}).get("title", "(deleted)"),
            "cover": (b or {}).get("url", ""),
            "borrowDate": doc.get("borrowDate"),
            "returnDate": doc.get("returnDate"),
            "renewCount": doc.get("renewCount", 0),
            "book": str(doc["book"]),
        })
    return render_template("loans.html", loans=view)

@app.route("/loans/make/<id>", methods=["POST","GET"])
@login_required
def make_loan(id):
    ok, msg, _loan = Loan.create(
        loans_coll=loans_coll,
        books_coll=books_coll,
        user_id=current_user.id,
        book_id=id,
        borrow_date=datetime.now(timezone.utc),
    )
    flash(msg, "success" if ok else "error")
    return redirect(url_for("book_details", id=id))

@app.route("/loans/renew/<loan_id>", methods=["POST","GET"])
@login_required
def renew_loan(loan_id):
    loan = Loan.get(loans_coll, loan_id)
    if not loan:
        flash("Loan not found.", "error")
        return redirect(url_for("my_loans"))
    if str(loan.doc["member"]) != current_user.id:
        abort(403)
    ok, msg = loan.renew()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("my_loans"))

@app.route("/loans/return/<id>", methods=["POST", "GET"])
@login_required
def return_loan(id):
    loan = Loan.get(loans_coll, id)
    if loan and str(loan.doc["member"]) == current_user.id:
        ok, msg = loan.mark_returned(books_coll)
        flash(msg, "success" if ok else "error")
        return redirect(url_for("my_loans"))

    try:
        uid = ObjectId(current_user.id); bid = ObjectId(id)
    except Exception:
        flash("Invalid identifier.", "error")
        return redirect(url_for("titles_page"))

    open_loan = loans_coll.find_one({
        "member": uid, "book": bid, "returnDate": {"$exists": False}
    })
    if not open_loan:
        flash("No open loan for this title.", "error")
        return redirect(url_for("book_details", id=id))

    loan = Loan(loans_coll, open_loan)
    ok, msg = loan.mark_returned(books_coll)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("book_details", id=id))

@app.route("/loans/delete/<loan_id>", methods=["POST","GET"])
@login_required
def delete_loan(loan_id):
    loan = Loan.get(loans_coll, loan_id)
    if not loan:
        flash("Loan not found.", "error")
        return redirect(url_for("my_loans"))
    if str(loan.doc["member"]) != current_user.id and not current_user.is_admin:
        abort(403)
    ok, msg = loan.delete()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("my_loans"))

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

# -------- New Book  --------
@app.route("/books/new", methods=["GET", "POST"])
@admin_required
def new_book():
    if not current_user.is_admin:
        flash("Only admin users can add new books.", "error")
        return redirect(url_for("titles_page"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        url_ = request.form.get("url", "").strip()
        description = request.form.get("description", "").splitlines()
        genres = request.form.getlist("genres")

        a_names, illustrators = [], []
        for i in range(1, 6):
            name = request.form.get(f"author{i}", "").strip()
            if name:
                a_names.append(name)
                if request.form.get(f"illus{i}") == "on":
                    illustrators.append(name)

        def as_int(v, default=0):
            try:
                return int(v)
            except Exception:
                return default
        pages = as_int(request.form.get("pages", "0"))
        copies = as_int(request.form.get("copies", "1"))

        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("new_book"))
        if category not in [c for c in CATEGORIES if c != "All"]:
            flash("Please choose a valid category.", "error")
            return redirect(url_for("new_book"))

        doc = {
            "title": title,
            "authors": a_names,
            "illustrators": illustrators,
            "category": category,
            "genres": genres,
            "url": url_,
            "description": description,
            "pages": pages,
            "copies": copies,
            "available": copies
        }
        try:
            books_coll.insert_one(doc)
            flash(f"“{title}” added successfully.", "success")
        except Exception as e:
            flash(f"Failed to add book: {e}", "error")

        return redirect(url_for("new_book"))

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
