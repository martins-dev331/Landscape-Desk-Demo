import json, os, secrets, sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

DB = os.environ.get("DB_PATH", "landscapedesk.db")
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")))
STATUSES = ["New", "Contacted", "Quote Sent", "Won", "Lost", "On Hold"]
QST = ["Draft", "Sent", "Accepted", "Declined", "Expired"]
CX = {"Easy": 1.0, "Standard": 1.15, "Hard": 1.35}  # labor multipliers; override the final price manually if needed
PUBLIC = {"login", "request_access"}
OPEN = "status not in ('Won','Lost') and follow!=''"
DEF_TERMS = "This quote is valid until the expiry date shown. The final price may change if the job scope changes. Payment terms are agreed before work begins."
DEF_REVIEW = "Hi [Customer Name], thank you for choosing [Business Name]. If you were happy with our work, we would really appreciate an honest review: [Review Link]"
DEF_REPLIES = [
 ("Pricing", "Service prices", "Hi [Customer Name], prices depend on the size and details of the job. Send us your address and what you need and we will prepare a quote. - [Business Name]"),
 ("Hours", "Opening hours", "Hi [Customer Name], our opening hours: [Opening Hours]. Tell us what time suits you."),
 ("Areas", "Service areas", "Hi [Customer Name], we serve: [Service Area]. Send your address and we will confirm."),
 ("Services", "Available services", "Hi [Customer Name], we offer: [Services]. Which one do you need?"),
 ("Booking", "Booking procedure", "Hi [Customer Name], to book, confirm the service, address and a preferred date and we will confirm a slot."),
 ("Payment", "Payment methods", "Hi [Customer Name], we will confirm accepted payment methods when we send your quote."),
 ("Estimates", "Free estimates", "Hi [Customer Name], we can prepare an estimate for your job. Please describe it and send the address."),
 ("Booking", "Appointment availability", "Hi [Customer Name], tell us two dates that suit you and we will confirm which is available."),
]
SCHEMA = """
create table if not exists users(id integer primary key, email text unique, pw text, role text default 'business', status text default 'pending', is_demo integer default 0, demo_expires text default '', created text);
create table if not exists settings(user_id integer primary key, biz_name text default '', logo text default '', phone text default '', email text default '', area text default '', hours text default '', currency text default '$', rate real default 0, markup real default 0, margin real default 20, quote_days integer default 14, review_link text default '', services text default '', terms text default '', review_tpl text default '');
create table if not exists leads(id integer primary key, user_id integer, name text, phone text, email text, service text, address text, notes text, follow text default '', value real default 0, status text default 'New', created text, is_demo integer default 0);
create table if not exists notes(id integer primary key, user_id integer, lead_id integer, text text, created text);
create table if not exists quotes(id integer primary key, user_id integer, lead_id integer, service text, inp text, labor real, mat real, other real, cost real, low real, rec real, high real, final real, status text default 'Draft', created text, sent text default '', expires text default '');
create table if not exists replies(id integer primary key, user_id integer, cat text, title text, body text);
create table if not exists reviews(id integer primary key, user_id integer, lead_id integer, status text, created text);
"""

def today(): return date.today().isoformat()
def now(): return datetime.now().isoformat(timespec="seconds")
def num(v, d=0.0):
    try: return float(v)
    except (TypeError, ValueError): return d
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db
@app.teardown_appcontext
def _close(e):
    d = g.pop("db", None)
    if d: d.close()
def q(sql, *a, one=False):
    c = db().execute(sql, a); r = c.fetchall(); db().commit()
    return (r[0] if r else None) if one else r
def ins(sql, *a):
    c = db().execute(sql, a); db().commit(); return c.lastrowid
def own(t, i):
    return q(f"select * from {t} where id=? and user_id=?", i, g.u["id"], one=True) or abort(404)

def ensure_account(uid, biz=""):
    if q("select 1 from settings where user_id=?", uid, one=True): return
    ins("insert into settings(user_id,biz_name,terms,review_tpl) values(?,?,?,?)", uid, biz, DEF_TERMS, DEF_REVIEW)
    for c, t, b in DEF_REPLIES: ins("insert into replies(user_id,cat,title,body) values(?,?,?,?)", uid, c, t, b)

def seed_demo(uid):
    if q("select 1 from leads where user_id=? and is_demo=1", uid, one=True): return
    for n, sv, st, off, val in [("Sample: Maria Lopez", "Lawn care", "New", 0, 350), ("Sample: Tom Becker", "Patio", "Contacted", -2, 4200),
                                ("Sample: Green Hills HOA", "Hedge trimming", "Quote Sent", 1, 1800), ("Sample: Ana Silva", "Garden design", "Won", -10, 2600)]:
        ins("insert into leads(user_id,name,service,status,follow,value,created,is_demo,notes) values(?,?,?,?,?,?,?,1,'Fictional demo record')",
            uid, n, sv, st, (date.today() + timedelta(days=off)).isoformat(), val, now())

def init_db():
    with sqlite3.connect(DB) as c: c.executescript(SCHEMA)
    with app.app_context():
        email, pw = os.environ.get("ADMIN_EMAIL", "").lower(), os.environ.get("ADMIN_PASSWORD", "")
        if email and len(pw) >= 10 and not q("select 1 from users where role='admin'", one=True):
            uid = ins("insert into users(email,pw,role,status,created) values(?,?, 'admin','active',?)", email, generate_password_hash(pw), now())
            ensure_account(uid, "Owner")

def problem(u):
    if u["status"] == "pending": return "Your access request is awaiting approval."
    if u["status"] != "active": return "Your access has been disabled. Contact the owner."
    if u["role"] != "admin" and u["is_demo"] and (u["demo_expires"] or "") < today(): return "Your demo access has expired. Contact the owner to continue."

DEMO_AUTO = os.environ.get("DEMO_AUTOLOGIN") == "1"

def showcase():
    """Recording mode: one fictional demo account, populated with sample data. Never an admin."""
    exp = (date.today() + timedelta(days=30)).isoformat()
    u = q("select * from users where email='demo@landscapedesk.local'", one=True)
    if u:
        q("update users set status='active',is_demo=1,demo_expires=? where id=?", exp, u["id"]); return u["id"]
    uid = ins("insert into users(email,pw,status,is_demo,demo_expires,created) values(?,?, 'active',1,?,?)",
              "demo@landscapedesk.local", generate_password_hash(secrets.token_hex(16)), exp, now())
    ensure_account(uid, "Green Valley Landscaping")
    q("update settings set phone='(555) 010-2030',area='North County and nearby towns',hours='Mon-Sat, 8am-6pm',rate=25,markup=10,margin=25,"
      "services='Lawn care|120\nGarden design|900\nPatio & paths|2500\nHedge trimming|180' where user_id=?", uid)
    d = lambda n: (date.today() + timedelta(days=n)).isoformat()
    ids = {}
    for n, sv, st, off, val, ph in [("Maria Lopez", "Lawn care", "New", 0, 350, "555-0101"), ("Tom Becker", "Patio & paths", "Contacted", -2, 4200, "555-0102"),
            ("Green Hills HOA", "Hedge trimming", "Quote Sent", 0, 1800, "555-0103"), ("Ana Silva", "Garden design", "Won", -10, 2600, "555-0104"),
            ("Kevin Brooks", "Lawn care", "Quote Sent", -1, 480, "555-0105"), ("Priya Shah", "Garden design", "Contacted", 2, 1500, "555-0106"),
            ("Dan Whitfield", "Patio & paths", "Lost", -6, 3900, "555-0107"), ("Olivia Grant", "Lawn care", "New", 3, 300, "555-0108")]:
        ids[n] = ins("insert into leads(user_id,name,phone,service,status,follow,value,created,is_demo,notes) values(?,?,?,?,?,?,?,?,1,'Fictional demo record')",
                     uid, n, ph, sv, st, d(off) if st not in ("Won", "Lost") else "", val, now())
    for who, sv, st, hrs, wk, mat in [("Green Hills HOA", "Hedge trimming", "Sent", 6, 2, 80), ("Ana Silva", "Garden design", "Accepted", 24, 2, 700),
                                      ("Kevin Brooks", "Lawn care", "Sent", 3, 1, 20), ("Dan Whitfield", "Patio & paths", "Declined", 40, 2, 1500), ("Tom Becker", "Patio & paths", "Draft", 36, 2, 1300)]:
        f = {"service": sv, "hours": hrs, "workers": wk, "rate": 25, "cx": "Standard", "materials": mat, "margin": 25, "desc": "Fictional demo quote."}
        c = calc(f, {"markup": 10})
        ins("insert into quotes(user_id,lead_id,service,inp,labor,mat,other,cost,low,rec,high,final,status,created,sent,expires) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            uid, ids[who], sv, json.dumps(f), c["labor"], c["mat"], c["other"], c["cost"], c["low"], c["rec"], c["high"], c["rec"], st, now(),
            d(-3) if st != "Draft" else "", d(11) if st == "Sent" else "")
    return uid

@app.before_request
def guard():
    session.setdefault("csrf", secrets.token_hex(16))
    if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), session["csrf"]): abort(400)
    g.u = g.s = None
    if request.endpoint in PUBLIC or request.endpoint in (None, "static"): return
    uid = session.get("uid")
    if not uid and DEMO_AUTO: uid = session["uid"] = showcase()
    u = q("select * from users where id=?", uid, one=True) if uid else None
    msg = problem(u) if u else "Please sign in."
    if msg:
        session.clear(); flash(msg); return redirect(url_for("login"))
    g.u = u; g.s = q("select * from settings where user_id=?", u["id"], one=True)

@app.context_processor
def ctx():
    return dict(u=g.get("u"), s=g.get("s"), statuses=STATUSES, qst=QST, today=today(),
                csrf=Markup(f'<input type=hidden name=csrf value="{session.get("csrf", "")}">'))

def admin(fn):
    @wraps(fn)
    def w(*a, **k):
        if g.u["role"] != "admin": abort(403)
        return fn(*a, **k)
    return w

def svcs(): return [x.split("|")[0].strip() for x in g.s["services"].splitlines() if x.strip()]
def fill(t, lead=None):
    s = g.s
    m = {"[Customer Name]": lead["name"] if lead else "[Customer Name]", "[Business Name]": s["biz_name"] or "[Business Name]",
         "[Service Area]": s["area"] or "[Service Area]", "[Opening Hours]": s["hours"] or "[Opening Hours]",
         "[Review Link]": s["review_link"] or "[Review Link]", "[Services]": ", ".join(svcs()) or "[Services]"}
    for k, v in m.items(): t = t.replace(k, v)
    return t

# ---------- auth ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if DEMO_AUTO: return redirect(url_for("dash"))
    if request.method == "POST":
        u = q("select * from users where email=?", request.form.get("email", "").strip().lower(), one=True)
        if not u or not check_password_hash(u["pw"], request.form.get("password", "")): flash("Wrong email or password.")
        elif problem(u): flash(problem(u))
        else:
            session.clear(); session["uid"] = u["id"]; session["csrf"] = secrets.token_hex(16)
            return redirect(url_for("dash"))
    return render_template("login.html")

@app.route("/request-access", methods=["GET", "POST"])
def request_access():
    if request.method == "POST":
        f = request.form; email = f.get("email", "").strip().lower()
        if "@" not in email or len(f.get("password", "")) < 10:
            flash("Enter a valid email and a password of at least 10 characters."); return redirect(request.url)
        if not q("select 1 from users where email=?", email, one=True):
            ensure_account(ins("insert into users(email,pw,status,created) values(?,?, 'pending',?)", email, generate_password_hash(f["password"]), now()), f.get("biz", "").strip())
        flash("Request received. The owner will review it."); return redirect(url_for("login"))
    return render_template("access.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect(url_for("login"))

# ---------- dashboard ----------
def stats(uid):
    q("update quotes set status='Expired' where user_id=? and status='Sent' and expires!='' and expires<?", uid, today())
    L = lambda w, *a: q(f"select count(*) c from leads where user_id=? {w}", uid, *a, one=True)["c"]
    Q = lambda w: q(f"select count(*) c, coalesce(sum(final),0) v from quotes where user_id=? {w}", uid, one=True)
    sent, acc = Q("and status!='Draft'"), Q("and status='Accepted'")
    won = q("select count(*) c, coalesce(sum(value),0) v from leads where user_id=? and status='Won'", uid, one=True)
    t = today()
    return dict(total=L(""), new=L("and status='New'"), due=L(f"and {OPEN} and follow=?", t), over=L(f"and {OPEN} and follow<?", t),
                sent=sent["c"], sent_v=sent["v"], wait=Q("and status='Sent'")["c"], won=won["c"], won_v=won["v"], lost=L("and status='Lost'"),
                rate=f'{acc["c"] / sent["c"] * 100:.0f}%' if sent["c"] else "n/a")

@app.route("/")
def dash():
    uid = g.u["id"]
    act = q("select 'Lead' k,name t,created d from leads where user_id=? union all select 'Quote',service,created from quotes where user_id=? "
            "union all select 'Note',text,created from notes where user_id=? order by d desc limit 8", uid, uid, uid)
    up = q(f"select * from leads where user_id=? and {OPEN} order by follow limit 5", uid)
    return render_template("dash.html", st=stats(uid), act=act, up=up)

# ---------- leads ----------
@app.route("/leads")
def leads():
    st, s = request.args.get("status", ""), request.args.get("q", "").strip()
    sql, a = "select * from leads where user_id=?", [g.u["id"]]
    if st in STATUSES: sql += " and status=?"; a.append(st)
    if s: sql += " and (name like ? or phone like ? or email like ? or service like ? or address like ?)"; a += [f"%{s}%"] * 5
    return render_template("leads.html", rows=q(sql + " order by created desc", *a), st=st, s=s)

@app.route("/followups")
def followups():
    uid, t = g.u["id"], today()
    return render_template("followups.html", over=q(f"select * from leads where user_id=? and {OPEN} and follow<? order by follow", uid, t),
                           due=q(f"select * from leads where user_id=? and {OPEN} and follow=?", uid, t))

@app.route("/leads/new", methods=["GET", "POST"])
@app.route("/leads/<int:i>", methods=["GET", "POST"])
def lead(i=None):
    l = own("leads", i) if i else None
    if request.method == "POST":
        f = request.form; name = f.get("name", "").strip()
        if not name: flash("Name is required."); return redirect(request.url)
        v = [name] + [f.get(k, "").strip() for k in ("phone", "email", "service", "address", "notes", "follow")]
        v += [f.get("status") if f.get("status") in STATUSES else "New", num(f.get("value"))]
        if l: q("update leads set name=?,phone=?,email=?,service=?,address=?,notes=?,follow=?,status=?,value=? where id=? and user_id=?", *v, i, g.u["id"])
        else: i = ins("insert into leads(user_id,name,phone,email,service,address,notes,follow,status,value,created) values(?,?,?,?,?,?,?,?,?,?,?)", g.u["id"], *v, now())
        flash("Saved."); return redirect(url_for("lead", i=i))
    uid = g.u["id"]
    return render_template("lead.html", l=l, notes=q("select * from notes where lead_id=? and user_id=? order by id desc", i, uid) if i else [],
                           qs=q("select * from quotes where lead_id=? and user_id=?", i, uid) if i else [], sv=svcs())

@app.route("/leads/<int:i>/note", methods=["POST"])
def lead_note(i):
    own("leads", i)
    if request.form.get("text", "").strip(): ins("insert into notes(user_id,lead_id,text,created) values(?,?,?,?)", g.u["id"], i, request.form["text"].strip(), now())
    return redirect(url_for("lead", i=i))

@app.route("/leads/<int:i>/delete", methods=["POST"])
def lead_delete(i):
    own("leads", i)
    for t in ("notes", "quotes", "reviews"): q(f"delete from {t} where lead_id=? and user_id=?", i, g.u["id"])
    q("delete from leads where id=? and user_id=?", i, g.u["id"]); flash("Lead deleted."); return redirect(url_for("leads"))

# ---------- quotes ----------
def calc(f, s):
    n = lambda k: max(num(f.get(k)), 0)
    labor = n("hours") * max(n("workers"), 1) * n("rate") * CX.get(f.get("cx"), 1.0)
    mat = n("materials") * (1 + s["markup"] / 100); other = n("equipment") + n("travel") + n("other")
    cost = labor + mat + other; m = min(n("margin"), 80)
    p = lambda x: cost / (1 - x / 100)
    return dict(labor=labor, mat=mat, other=other, cost=cost, low=p(max(m - 5, 0)), rec=p(m), high=p(min(m + 10, 90)))

QF = ["service", "desc", "size", "cx", "hours", "workers", "rate", "materials", "equipment", "travel", "other", "margin", "override"]

@app.route("/quotes")
def quotes():
    stats(g.u["id"]); st, s = request.args.get("status", ""), request.args.get("q", "").strip()
    sql, a = "select q.*,l.name from quotes q join leads l on l.id=q.lead_id where q.user_id=?", [g.u["id"]]
    if st in QST: sql += " and q.status=?"; a.append(st)
    if s: sql += " and (l.name like ? or q.service like ?)"; a += [f"%{s}%"] * 2
    return render_template("quotes.html", rows=q(sql + " order by q.id desc", *a), st=st, s=s)

@app.route("/quotes/new", methods=["GET", "POST"])
@app.route("/quotes/<int:i>/edit", methods=["GET", "POST"])
def quote_form(i=None):
    qt = own("quotes", i) if i else None
    if request.method == "POST":
        f = request.form; lid = int(num(f.get("lead_id"))); own("leads", lid)
        c = calc(f, g.s); final = num(f.get("override")) or c["rec"]
        inp = json.dumps({k: f.get(k, "") for k in QF}); sv = f.get("service", "").strip() or "Service"
        vals = (sv, inp, c["labor"], c["mat"], c["other"], c["cost"], c["low"], c["rec"], c["high"], final)
        if qt: q("update quotes set service=?,inp=?,labor=?,mat=?,other=?,cost=?,low=?,rec=?,high=?,final=?,lead_id=? where id=? and user_id=?", *vals, lid, i, g.u["id"])
        else: i = ins("insert into quotes(service,inp,labor,mat,other,cost,low,rec,high,final,user_id,lead_id,created) values(?,?,?,?,?,?,?,?,?,?,?,?,?)", *vals, g.u["id"], lid, now())
        flash("Quote saved."); return redirect(url_for("quote_view", i=i))
    d = json.loads(qt["inp"]) if qt else {"rate": g.s["rate"], "margin": g.s["margin"], "cx": "Standard", "workers": 1}
    return render_template("qform.html", qt=qt, d=d, cxs=list(CX), lid=(qt["lead_id"] if qt else request.args.get("lead", type=int)),
                           ls=q("select id,name from leads where user_id=? order by name", g.u["id"]), sv=svcs())

@app.route("/quotes/<int:i>")
def quote_view(i):
    qt = own("quotes", i); lead = own("leads", qt["lead_id"]); s = g.s; d = json.loads(qt["inp"])
    exp = qt["expires"] or (date.today() + timedelta(days=s["quote_days"])).isoformat()
    msg = (f'Hi {lead["name"]}, thank you for contacting {s["biz_name"] or "us"}. Our quote for {qt["service"]} is {s["currency"]}{qt["final"]:,.2f}. '
           f'{d.get("desc", "")} This quote is valid until {exp}. Reply to book or ask questions. {s["phone"]}').replace("  ", " ")
    return render_template("qview.html", qt=qt, lead=lead, d=d, exp=exp, msg=msg)

@app.route("/quotes/<int:i>/status", methods=["POST"])
def quote_status(i):
    qt = own("quotes", i); st = request.form.get("status")
    if st not in QST: abort(400)
    if st == "Sent":
        q("update quotes set status=?,sent=?,expires=? where id=? and user_id=?", st, today(), (date.today() + timedelta(days=g.s["quote_days"])).isoformat(), i, g.u["id"])
        q("update leads set status='Quote Sent' where id=? and user_id=? and status in ('New','Contacted')", qt["lead_id"], g.u["id"])
    else: q("update quotes set status=? where id=? and user_id=?", st, i, g.u["id"])
    flash("Status updated."); return redirect(url_for("quote_view", i=i))

@app.route("/quotes/<int:i>/won", methods=["POST"])
def quote_won(i):
    qt = own("quotes", i)
    q("update leads set status='Won',value=? where id=? and user_id=?", qt["final"], qt["lead_id"], g.u["id"]); flash("Lead marked Won."); return redirect(url_for("quote_view", i=i))

@app.route("/quotes/<int:i>/delete", methods=["POST"])
def quote_delete(i):
    own("quotes", i); q("delete from quotes where id=? and user_id=?", i, g.u["id"]); flash("Quote deleted."); return redirect(url_for("quotes"))

# ---------- replies, reviews, settings ----------
@app.route("/replies", methods=["GET", "POST"])
def replies():
    uid = g.u["id"]
    if request.method == "POST":
        f = request.form; act = f.get("act"); rid = int(num(f.get("id")))
        if act == "delete": own("replies", rid); q("delete from replies where id=? and user_id=?", rid, uid)
        elif f.get("title", "").strip() and f.get("body", "").strip():
            if act == "edit": own("replies", rid); q("update replies set cat=?,title=?,body=? where id=? and user_id=?", f.get("cat", "").strip(), f["title"].strip(), f["body"].strip(), rid, uid)
            else: ins("insert into replies(user_id,cat,title,body) values(?,?,?,?)", uid, f.get("cat", "").strip(), f["title"].strip(), f["body"].strip())
        return redirect(url_for("replies"))
    s = request.args.get("q", "").strip(); lid = request.args.get("lead", type=int)
    lead = own("leads", lid) if lid else None
    rows = q("select * from replies where user_id=? and (title like ? or body like ? or cat like ?) order by cat,title", uid, *[f"%{s}%"] * 3)
    return render_template("replies.html", rows=[dict(r, shown=fill(r["body"], lead)) for r in rows], s=s, lid=lid, ls=q("select id,name from leads where user_id=? order by name", uid))

@app.route("/reviews", methods=["GET", "POST"])
def reviews():
    uid = g.u["id"]
    if request.method == "POST":
        lid = int(num(request.form.get("lead_id"))); own("leads", lid)
        ins("insert into reviews(user_id,lead_id,status,created) values(?,?,?,?)", uid, lid, "Sent" if request.form.get("status") == "Sent" else "Prepared", now())
        flash("Recorded."); return redirect(url_for("reviews", lead=lid))
    lid = request.args.get("lead", type=int); lead = own("leads", lid) if lid else None
    return render_template("reviews.html", lead=lead, msg=fill(g.s["review_tpl"], lead) if lead else "", ls=q("select id,name from leads where user_id=? order by name", uid),
                           log=q("select r.*,l.name from reviews r join leads l on l.id=r.lead_id where r.user_id=? order by r.id desc limit 20", uid))

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        f = request.form; t = lambda k: f.get(k, "").strip()
        q("update settings set biz_name=?,logo=?,phone=?,email=?,area=?,hours=?,currency=?,rate=?,markup=?,margin=?,quote_days=?,review_link=?,services=?,terms=?,review_tpl=? where user_id=?",
          t("biz_name"), t("logo"), t("phone"), t("email"), t("area"), t("hours"), t("currency") or "$", num(f.get("rate")), num(f.get("markup")), num(f.get("margin"), 20),
          int(num(f.get("quote_days"), 14)), t("review_link"), t("services"), t("terms"), t("review_tpl"), g.u["id"])
        flash("Settings saved."); return redirect(url_for("settings"))
    return render_template("settings.html")

# ---------- owner/admin ----------
@app.route("/admin")
@admin
def admin_page():
    return render_template("admin.html", users=q("select u.*,s.biz_name from users u left join settings s on s.user_id=u.id order by u.id desc"))

@app.route("/admin/create", methods=["POST"])
@admin
def admin_create():
    f = request.form; email = f.get("email", "").strip().lower()
    if "@" not in email or len(f.get("password", "")) < 10 or q("select 1 from users where email=?", email, one=True):
        flash("Need a new email and a password of at least 10 characters."); return redirect(url_for("admin_page"))
    demo = f.get("mode") == "demo"; exp = (date.today() + timedelta(days=int(num(f.get("days"), 7)))).isoformat() if demo else ""
    uid = ins("insert into users(email,pw,status,is_demo,demo_expires,created) values(?,?, 'active',?,?,?)", email, generate_password_hash(f["password"]), int(demo), exp, now())
    ensure_account(uid, f.get("biz", "").strip())
    if demo: seed_demo(uid)
    flash("Account created."); return redirect(url_for("admin_page"))

@app.route("/admin/<int:i>/<act>", methods=["POST"])
@admin
def admin_act(i, act):
    t = q("select * from users where id=?", i, one=True) or abort(404)
    if t["role"] == "admin": flash("Admin accounts can't be changed here."); return redirect(url_for("admin_page"))
    f = request.form
    if act == "approve":
        demo = f.get("mode") == "demo"; exp = (date.today() + timedelta(days=int(num(f.get("days"), 7)))).isoformat() if demo else ""
        q("update users set status='active',is_demo=?,demo_expires=? where id=?", int(demo), exp, i)
        if demo: seed_demo(i)
    elif act == "paying": q("update users set status='active',is_demo=0,demo_expires='' where id=?", i)
    elif act == "reject": q("update users set status='rejected' where id=?", i)
    elif act in ("disable", "revoke"): q("update users set status='disabled' where id=?", i)
    elif act == "enable": q("update users set status='active' where id=?", i)
    elif act == "expiry": q("update users set demo_expires=?,is_demo=1 where id=?", f.get("date", ""), i)
    else: abort(404)
    flash("Updated."); return redirect(url_for("admin_page"))

from templates import T  # noqa: E402
app.jinja_env.loader = DictLoader(T)
init_db()
if __name__ == "__main__": app.run(debug=True)
