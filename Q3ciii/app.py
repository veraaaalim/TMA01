from flask import (
    Flask, render_template, request, abort,
    url_for, redirect, flash
)
from pymongo import MongoClient, errors, ReturnDocument
from bson import ObjectId
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import random
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
loans_coll = db["loans"]

# ---- Seed Books ----
from books import all_books

def seed_books_if_empty(coll, all_books):
    if coll.estimated_document_count() == 0:
        docs = []
        for b in all_books:
            copies = int(b.get("copies", 0))
            docs.append({
                "title": b.get("title", "").strip(),
                "authors": b.get("authors", []),
                "category": b.get("category", ""),
                "genres": b.get("genres", []),
                "url": b.get("url", ""),
                "description": b.get("description", []),
                "pages": b.get("pages", 0),
                "copies": copies,
                "available": copies
            })
        if docs:
            coll.insert_many(docs)
            coll.create_index("title")
            coll.create_index("category")
            coll.create_index([("title", "text"),
                               ("authors", "text"),
                               ("genres", "text"),
                               ("description", "text")])
            print("[MongoDB] Seeded books and created indexes")

try:
    seed_books_if_empty(books_coll, all_books)
except Exception as e:
    print(f"[Seed WARNING] {e}", file=sys.stderr)

# ---- Seed default users ----
def seed_users_if_empty():
    if users_coll.estimated_document_count() == 0:
        users_coll.insert_many([
            {"email":"admin@lib.sg","password":generate_password_hash("12345"),"name":"Admin","is_admin":True},
            {"email":"poh@lib.sg","password":generate_password_hash("12345"),"name":"Peter Oh","is_admin":False}
        ])
        users_coll.create_index("email", unique=True)
        print("[MongoDB] Seeded default users")

try:
    seed_users_if_empty()
except Exception as e:
    print(f"[User Seed WARNING] {e}", file=sys.stderr)

# ---- Indexes for loans ----
try:
    loans_coll.create_index([("user_id", 1), ("book_id", 1), ("returned", 1)])
except Exception as e:
    print(f"[Loans Index WARNING] {e}", file=sys.stderr)

# -------- Auth (Flask-Login) --------
login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.unauthorized_handler
def _unauth():
    flash("Please login or register first to get an account", "info")
    return redirect(url_for("login", next=request.path))

class User(UserMixin):
    def __init__(self, doc):
        self.id = str(doc["_id"])
        self.email = doc["email"]
        self.name = doc.get("name", "")
        self.is_admin = bool(doc.get("is_admin", False))

@login_manager.user_loader
def load_user(user_id):
    try:
        oid = ObjectId(user_id)
    except Exception:
        return None
    doc = users_coll.find_one({"_id": oid})
    return User(doc) if doc else None

# -------- Helpers --------
CATEGORIES = ["All", "Children", "Teens", "Adult"]

def first_last(paras):
    parts = [p.strip() for p in paras if p and p.strip()]
    if not parts: return ""
    return parts[0] if len(parts) == 1 else f"{parts[0]}\n\n{parts[-1]}"

def to_oid(id_str):
    try:
        return ObjectId(id_str)
    except Exception:
        return None

def clamp_to_today(dt: datetime) -> datetime:
    now = datetime.now()
    return dt if dt <= now else now

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
        "category": 1, "genres": 1, "pages": 1,
        "description": 1, "copies": 1, "available": 1
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
        copies = int(d.get("copies", 0) or 0)
        available = int(d.get("available", 0) or 0)
        cards.append({
            "_id": str(d["_id"]),
            "title": d.get("title", ""),
            "authors": ", ".join(d.get("authors", [])),
            "img": d.get("url", ""),
            "category_line": f"{d.get('category', '')}, " + ", ".join(d.get("genres", [])),
            "pages": d.get("pages", 0),
            "short_desc": first_last(d.get("description", [])),
            "copies": copies,
            "available": available
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
    oid = to_oid(id)
    if not oid: abort(404)
    d = books_coll.find_one({"_id": oid})
    if not d: abort(404)
    d["_id"] = str(d["_id"])
    return render_template("book_details.html", book=d)

# -------- Loans --------
RENEW_DAYS = 14

@app.route("/loans/make/<id>", methods=["POST", "GET"])
@login_required
def make_loan(id):
    book_oid = to_oid(id)
    if not book_oid:
        flash("Invalid book id.", "error")
        return redirect(url_for("titles_page"))

    book = books_coll.find_one({"_id": book_oid}, {"title": 1, "available": 1})
    if not book:
        flash("Book not found.", "error")
        return redirect(url_for("titles_page"))

    active = loans_coll.find_one({
        "user_id": ObjectId(current_user.id),
        "book_id": book_oid,
        "returned": False
    })
    if active:
        flash("You already have this title on loan. Please return it before borrowing again.", "warning")
        return redirect(url_for("book_details", id=id))

    # Decrement availability atomically
    updated = books_coll.find_one_and_update(
        {"_id": book_oid, "available": {"$gt": 0}},
        {"$inc": {"available": -1}},
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        flash("No copies available to loan.", "error")
        return redirect(url_for("book_details", id=id))

    now = datetime.now()
    loans_coll.insert_one({
        "user_id": ObjectId(current_user.id),
        "book_id": book_oid,
        "title": book.get("title", ""),
        "borrow_date": now,
        "due_date": now + timedelta(days=RENEW_DAYS),  
        "return_date": None,
        "renew_count": 0,
        "returned": False
    })

    flash("Loan created successfully. Enjoy your book!", "success")
    return redirect(url_for("my_loans"))

@app.route("/loans")
@login_required
def my_loans():
    rows = loans_coll.find({"user_id": ObjectId(current_user.id)}).sort([
        ("borrow_date", -1), ("borrowDate", -1)
    ])

    items = []
    today = datetime.now().date()

    for r in rows:
        borrow_dt = r.get("borrow_date") or r.get("borrowDate")
        due_dt    = r.get("due_date")
        return_dt = r.get("return_date") or r.get("returnDate")
        renew_cnt = int(r.get("renew_count", r.get("renewCount", 0)) or 0)
        returned  = bool(r.get("returned", False))

        if not due_dt and borrow_dt:
            due_dt = borrow_dt + timedelta(days=RENEW_DAYS)
            if not returned and due_dt < datetime.now():
                due_dt = datetime.now() + timedelta(days=RENEW_DAYS)
            loans_coll.update_one({"_id": r["_id"]}, {"$set": {"due_date": due_dt}})

        overdue = (not returned) and bool(due_dt) and (today > due_dt.date())

        book_doc = books_coll.find_one({"_id": r.get("book_id")}, {"authors": 1, "url": 1}) if r.get("book_id") else None
        authors  = ", ".join((book_doc or {}).get("authors", []))
        thumb    = (book_doc or {}).get("url", "")

        items.append({
            "_id": str(r["_id"]),
            "book_id": str(r.get("book_id")) if r.get("book_id") else "",
            "title": r.get("title", ""),
            "authors": authors,
            "thumb": thumb,
            "borrow_date": borrow_dt,
            "due_date": due_dt.date() if due_dt else None,
            "return_date": return_dt.date() if return_dt else None,
            "renew_count": renew_cnt,
            "returned": returned,
            "overdue": bool(overdue),
        })

    return render_template("loans.html", loans=items)


@app.route("/loans/return/<id>", methods=["POST", "GET"])
@login_required
def return_loan(id):
    oid = to_oid(id)
    if not oid:
        flash("Invalid id.", "error")
        return redirect(url_for("my_loans"))

    loan = loans_coll.find_one({
        "$or": [{"_id": oid}, {"book_id": oid}],
        "user_id": ObjectId(current_user.id),
        "returned": False
    })
    if not loan:
        flash("No active loan to return.", "warning")
        return redirect(url_for("my_loans"))

    now = datetime.now()

    borrow_dt = loan.get("borrow_date") or loan.get("borrowDate") or now

    due_dt = loan.get("due_date")
    if isinstance(due_dt, str):
        try:
            due_dt = datetime.fromisoformat(due_dt)
        except Exception:
            due_dt = None

    cap_latest = min(now, due_dt) if due_dt else now
    if cap_latest < borrow_dt:
        cap_latest = borrow_dt

    if cap_latest > borrow_dt:
        frac = random.random()
        ret_dt = borrow_dt + (cap_latest - borrow_dt) * frac
    else:
        ret_dt = cap_latest  

    loans_coll.update_one(
        {"_id": loan["_id"]},
        {"$set": {"returned": True, "return_date": ret_dt, "returnDate": ret_dt}}
    )

    # Increment book availability
    if loan.get("book_id"):
        books_coll.update_one({"_id": loan["book_id"]}, {"$inc": {"available": 1}})

    flash(f"Book returned. Return date: {ret_dt.strftime('%d %b %Y %H:%M')}", "success")
    return redirect(url_for("my_loans"))


RENEW_DAYS_MIN = 10
RENEW_DAYS_MAX = 20

@app.route("/loans/<loan_id>/renew", methods=["GET", "POST"])
@login_required
def renew_loan(loan_id):
    loan = loans_coll.find_one({
        "_id": ObjectId(loan_id),
        "user_id": ObjectId(current_user.id)  
    })
    if not loan:
        flash("Loan not found.", "error")
        return redirect(url_for("my_loans"))

    if loan.get("returned"):
        flash("This loan was already returned.", "error")
        return redirect(url_for("my_loans"))

    renew_count = int(loan.get("renew_count", 0))
    if renew_count >= 2:
        flash("You have reached the maximum of 2 renewals.", "error")
        return redirect(url_for("my_loans"))

    now = datetime.now()

    due = loan.get("due_date")
    if not isinstance(due, datetime):
        if isinstance(due, str):
            try:
                due = datetime.fromisoformat(due)
            except Exception:
                due = None
        if due is None:
            borrow = loan.get("borrow_date") or now
            fallback_days = random.randint(RENEW_DAYS_MIN, RENEW_DAYS_MAX)
            due = borrow + timedelta(days=fallback_days)

    base = due if due > now else now

    extra_days = random.randint(RENEW_DAYS_MIN, RENEW_DAYS_MAX)
    new_due = base + timedelta(days=extra_days)

    result = loans_coll.update_one(
        {"_id": loan["_id"]},
        {
            "$set": {"due_date": new_due, "updated_at": now},
            "$inc": {"renew_count": 1}
        }
    )

    if result.modified_count == 1:
        flash(f"Renewed. New due date: {new_due.strftime('%d %b %Y')} (+{extra_days} days)", "success")
    else:
        flash("Renew failed. Please try again.", "error")

    return redirect(url_for("my_loans"))



@app.route("/loans/delete/<loan_id>", methods=["POST", "GET"])
@login_required
def delete_loan(loan_id):
    """Delete only returned loans (user keeps history until manually removed)."""
    try:
        oid = ObjectId(loan_id)
    except Exception:
        flash("Invalid loan id.", "error")
        return redirect(url_for("my_loans"))

    loan = loans_coll.find_one({"_id": oid, "user_id": ObjectId(current_user.id)})
    if not loan:
        flash("Loan not found.", "error")
        return redirect(url_for("my_loans"))

    if not loan.get("returned", False) and not loan.get("returnDate"):
        flash("You can delete only returned loans.", "warning")
        return redirect(url_for("my_loans"))

    loans_coll.delete_one({"_id": oid})
    flash("Loan deleted.", "info")
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
            nxt = request.args.get("next")
            return redirect(nxt or url_for("titles_page"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("titles_page"))

# -------- Main --------
if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8080"))
    app.run(host=host, port=port, debug=True)
