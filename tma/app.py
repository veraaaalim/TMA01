from flask import Flask, render_template, request, abort, url_for, redirect
from books import all_books

app = Flask(__name__)

CATEGORIES = ["All", "Children", "Teens", "Adult"]

def first_last(paras):
    parts = [p.strip() for p in paras if p and p.strip()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{parts[0]}\n\n{parts[-1]}"

@app.route("/")
def home():
    # Open on Book Titles page
    return redirect(url_for("titles_page"))

@app.route("/titles")
def titles_page():
    selected = request.args.get("category", "All")
    books_sorted = sorted(all_books, key=lambda b: b["title"].lower())
    if selected != "All":
        books_sorted = [b for b in books_sorted if b["category"] == selected]

    # transform for template
    cards = [{
        "title": b["title"],
        "authors": ", ".join(b["authors"]),
        "img": b["url"],
        "category_line": f"{b['category']}, " + ", ".join(b["genres"]),
        "pages": b["pages"],
        "short_desc": first_last(b["description"])
    } for b in books_sorted]

    return render_template(
        "layout.html",
        total=len(cards),
        cards=cards,
        categories=CATEGORIES,
        selected=selected
    )

@app.route("/book/<title>")
def book_details(title):
    book = next((b for b in all_books if b["title"] == title), None)
    if not book:
        abort(404)
    return render_template("book_details.html", book=book)

if __name__ == "__main__":
    app.run(debug=True)
