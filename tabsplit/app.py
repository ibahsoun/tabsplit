import os
import secrets

from flask import Flask, jsonify, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes


def database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        # Render hands out postgres://, SQLAlchemy 2 wants postgresql://
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url
    path = os.environ.get("SQLITE_PATH")
    if not path:
        disk = "/var/data"
        folder = disk if os.path.isdir(disk) and os.access(disk, os.W_OK) else BASE_DIR
        path = os.path.join(folder, "tabsplit.db")
    return "sqlite:///" + path


app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SQLALCHEMY_DATABASE_URI"] = database_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

expense_shares = db.Table(
    "expense_shares",
    db.Column("expense_id", db.Integer, db.ForeignKey("expense.id", ondelete="CASCADE"), primary_key=True),
    db.Column("member_id", db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), primary_key=True),
)


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="$")
    members = db.relationship("Member", backref="group", cascade="all, delete-orphan", lazy="selectin")
    expenses = db.relationship("Expense", backref="group", cascade="all, delete-orphan", lazy="selectin")
    items = db.relationship("Item", backref="group", cascade="all, delete-orphan", lazy="selectin")


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id", ondelete="CASCADE"), nullable=False, index=True)
    payer_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False)
    label = db.Column(db.String(160), nullable=False)
    cents = db.Column(db.Integer, nullable=False)
    shared_with = db.relationship("Member", secondary=expense_shares, lazy="selectin")


class Item(db.Model):
    """Something a person physically brings: ice, speaker, charcoal, plates."""
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False)
    label = db.Column(db.String(160), nullable=False)
    qty = db.Column(db.String(40), nullable=False, default="")
    cents = db.Column(db.Integer, nullable=False, default=0)   # what it cost them, 0 = free / already owned
    counted = db.Column(db.Boolean, nullable=False, default=False)  # include cost in the money split
    packed = db.Column(db.Boolean, nullable=False, default=False)   # ticked off on the day


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def new_code():
    for _ in range(30):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not Group.query.filter_by(code=code).first():
            return code
    return secrets.token_hex(5).upper()


def to_cents(value):
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def get_group_or_404(code):
    group = Group.query.filter(func.upper(Group.code) == code.upper()).first()
    if group is None:
        return None
    return group


def member_in_group(group, member_id):
    return next((m for m in group.members if m.id == member_id), None)


def contributions(group):
    """Every money movement, whether it came from the expense list or a priced item."""
    rows = []
    for e in group.expenses:
        share_ids = [m.id for m in e.shared_with] or [m.id for m in group.members]
        rows.append({"payer_id": e.payer_id, "cents": e.cents, "share_ids": share_ids})
    for i in group.items:
        if i.counted and i.cents > 0:
            rows.append({"payer_id": i.member_id, "cents": i.cents,
                         "share_ids": [m.id for m in group.members]})
    return rows


def balances(group):
    """paid - owed, in cents, per member. Remainder cents go to the earliest ids so nothing is lost."""
    bal = {m.id: 0 for m in group.members}
    paid = {m.id: 0 for m in group.members}
    owed = {m.id: 0 for m in group.members}

    for row in contributions(group):
        ids = sorted(pid for pid in row["share_ids"] if pid in bal)
        if not ids:
            continue
        base, rem = divmod(row["cents"], len(ids))
        for idx, pid in enumerate(ids):
            cut = base + (1 if idx < rem else 0)
            owed[pid] += cut
            bal[pid] -= cut
        if row["payer_id"] in bal:
            paid[row["payer_id"]] += row["cents"]
            bal[row["payer_id"]] += row["cents"]
    return bal, paid, owed


def transfers(bal):
    """Greedy settle-up: fewest possible payments to zero everyone out."""
    debtors = sorted([[k, -v] for k, v in bal.items() if v < 0], key=lambda x: -x[1])
    creditors = sorted([[k, v] for k, v in bal.items() if v > 0], key=lambda x: -x[1])
    out, i, j = [], 0, 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        if amount > 0:
            out.append({"from_id": debtors[i][0], "to_id": creditors[j][0], "cents": amount})
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return out


def serialize(group):
    bal, paid, owed = balances(group)
    names = {m.id: m.name for m in group.members}
    total = sum(r["cents"] for r in contributions(group))
    head_count = len(group.members)
    return {
        "code": group.code,
        "name": group.name,
        "currency": group.currency,
        "members": [
            {
                "id": m.id,
                "name": m.name,
                "paid": paid.get(m.id, 0),
                "owed": owed.get(m.id, 0),
                "balance": bal.get(m.id, 0),
            }
            for m in sorted(group.members, key=lambda x: x.id)
        ],
        "expenses": [
            {
                "id": e.id,
                "label": e.label,
                "cents": e.cents,
                "payer_id": e.payer_id,
                "payer": names.get(e.payer_id, "?"),
                "share_ids": sorted(m.id for m in e.shared_with) or sorted(names),
            }
            for e in sorted(group.expenses, key=lambda x: -x.id)
        ],
        "items": [
            {
                "id": i.id,
                "label": i.label,
                "qty": i.qty,
                "cents": i.cents,
                "counted": i.counted,
                "packed": i.packed,
                "member_id": i.member_id,
                "member": names.get(i.member_id, "?"),
            }
            for i in sorted(group.items, key=lambda x: -x.id)
        ],
        "transfers": [
            {**t, "from": names.get(t["from_id"], "?"), "to": names.get(t["to_id"], "?")}
            for t in transfers(bal)
        ],
        "total": total,
        "per_head": total // head_count if head_count else 0,
    }


def state(group):
    return jsonify(serialize(group))


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/")
@app.get("/g/<code>")
def index(code=None):
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.post("/api/groups")
def create_group():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "Our outing"
    currency = (data.get("currency") or "$").strip()[:8] or "$"
    group = Group(code=new_code(), name=name[:120], currency=currency)
    db.session.add(group)

    for raw in (data.get("members") or []):
        person = str(raw).strip()
        if person:
            group.members.append(Member(name=person[:80]))
    db.session.commit()
    return state(group), 201


@app.get("/api/groups/<code>")
def read_group(code):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code. Check the letters and try again."}, 404
    return state(group)


@app.patch("/api/groups/<code>")
def update_group(code):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    data = request.get_json(silent=True) or {}
    if "name" in data and str(data["name"]).strip():
        group.name = str(data["name"]).strip()[:120]
    if "currency" in data and str(data["currency"]).strip():
        group.currency = str(data["currency"]).strip()[:8]
    db.session.commit()
    return state(group)


@app.post("/api/groups/<code>/members")
def add_members(code):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    data = request.get_json(silent=True) or {}
    raw = data.get("names") or ([data.get("name")] if data.get("name") else [])
    existing = {m.name.lower() for m in group.members}
    added = 0
    for entry in raw:
        person = str(entry or "").strip()
        if person and person.lower() not in existing:
            group.members.append(Member(name=person[:80]))
            existing.add(person.lower())
            added += 1
    if not added:
        return {"error": "Nothing to add — those names are already on the list."}, 400
    db.session.commit()
    return state(group), 201


@app.delete("/api/groups/<code>/members/<int:member_id>")
def remove_member(code, member_id):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    member = member_in_group(group, member_id)
    if not member:
        return {"error": "That person isn't in this group."}, 404
    if any(e.payer_id == member_id for e in group.expenses) or any(i.member_id == member_id for i in group.items):
        return {"error": f"{member.name} still has entries. Delete those first."}, 400
    for e in group.expenses:
        e.shared_with = [m for m in e.shared_with if m.id != member_id]
    db.session.delete(member)
    db.session.commit()
    return state(group)


@app.post("/api/groups/<code>/expenses")
def add_expense(code):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    data = request.get_json(silent=True) or {}

    label = (data.get("label") or "").strip()
    if not label:
        return {"error": "Give the expense a name, like 'Taxi' or 'Meat'."}, 400

    cents = to_cents(data.get("amount"))
    if cents is None or cents <= 0:
        return {"error": "Enter an amount above zero."}, 400

    payer = member_in_group(group, data.get("payer_id"))
    if not payer:
        return {"error": "Pick who paid."}, 400

    ids = data.get("share_ids")
    if ids:
        sharers = [m for m in group.members if m.id in set(ids)]
    else:
        sharers = list(group.members)
    if not sharers:
        return {"error": "Pick at least one person to split this between."}, 400

    expense = Expense(group_id=group.id, payer_id=payer.id, label=label[:160], cents=cents)
    expense.shared_with = sharers
    db.session.add(expense)
    db.session.commit()
    return state(group), 201


@app.delete("/api/groups/<code>/expenses/<int:expense_id>")
def remove_expense(code, expense_id):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    expense = next((e for e in group.expenses if e.id == expense_id), None)
    if not expense:
        return {"error": "That expense is already gone."}, 404
    db.session.delete(expense)
    db.session.commit()
    return state(group)


@app.post("/api/groups/<code>/items")
def add_item(code):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    data = request.get_json(silent=True) or {}

    label = (data.get("label") or "").strip()
    if not label:
        return {"error": "Name the thing being brought."}, 400

    member = member_in_group(group, data.get("member_id"))
    if not member:
        return {"error": "Pick who is bringing it."}, 400

    cents = to_cents(data.get("cents_input") or data.get("amount") or 0) or 0
    cents = max(cents, 0)
    counted = bool(data.get("counted")) and cents > 0

    item = Item(group_id=group.id, member_id=member.id, label=label[:160],
                qty=str(data.get("qty") or "").strip()[:40], cents=cents, counted=counted)
    db.session.add(item)
    db.session.commit()
    return state(group), 201


@app.patch("/api/groups/<code>/items/<int:item_id>")
def update_item(code, item_id):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    item = next((i for i in group.items if i.id == item_id), None)
    if not item:
        return {"error": "That item is already gone."}, 404
    data = request.get_json(silent=True) or {}
    if "packed" in data:
        item.packed = bool(data["packed"])
    if "counted" in data:
        item.counted = bool(data["counted"]) and item.cents > 0
    db.session.commit()
    return state(group)


@app.delete("/api/groups/<code>/items/<int:item_id>")
def remove_item(code, item_id):
    group = get_group_or_404(code)
    if not group:
        return {"error": "No group with that code."}, 404
    item = next((i for i in group.items if i.id == item_id), None)
    if not item:
        return {"error": "That item is already gone."}, 404
    db.session.delete(item)
    db.session.commit()
    return state(group)


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
