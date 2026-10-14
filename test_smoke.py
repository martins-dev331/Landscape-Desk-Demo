import os, tempfile
os.environ.update(DB_PATH=tempfile.mktemp(), ADMIN_EMAIL="owner@x.com", ADMIN_PASSWORD="owner-pass-123")
import app as A
from datetime import date, timedelta
def cl():
    c = A.app.test_client(); c.get("/login"); return c
def post(c, url, **d):
    with c.session_transaction() as s: d["csrf"] = s["csrf"]
    return c.post(url, data=d, follow_redirects=True)
ok = lambda n, v: print(("PASS " if v else "FAIL ") + n)
a = cl(); ok("anon redirected", a.get("/leads").status_code == 302)
ok("post without csrf rejected", a.post("/login", data={"email": "x"}).status_code == 400)
ok("admin login", b"Dashboard" in post(a, "/login", email="owner@x.com", password="owner-pass-123").data)
b = cl(); post(b, "/request-access", biz="Biz B", email="b@x.com", password="bpassword123")
ok("pending blocked", b"awaiting approval" in post(b, "/login", email="b@x.com", password="bpassword123").data)
post(a, "/admin/2/approve", mode="demo", days="7")
ok("demo login", b"Demo account" in post(b, "/login", email="b@x.com", password="bpassword123").data)
ok("demo has fictional leads", b"Sample: Maria" in b.get("/leads").data)
post(b, "/leads/new", name="Real Customer", status="New", follow=date.today().isoformat(), value="0")
ok("lead saved", b"Real Customer" in b.get("/leads").data)
r = post(b, "/quotes/new", lead_id="5", service="Lawn", hours="4", workers="2", rate="20", cx="Standard", materials="100", equipment="10", travel="5", other="0", margin="20")
exp = (4*2*20*1.15 + 100 + 15) / 0.8
ok(f"quote math ({exp:.2f})", f"{exp:,.2f}".encode() in r.data)
ok("dashboard live stats", b"Follow-ups today" in b.get("/").data and b.get("/followups").data.count(b"Real Customer") == 1)
c = cl(); post(c, "/admin/create", biz="C")  # not logged in
post(a, "/admin/create", biz="Biz C", email="c@x.com", password="cpassword123", mode="real", days="7")
post(c, "/login", email="c@x.com", password="cpassword123")
ok("tenant isolation (lead)", c.get("/leads/5").status_code == 404)
ok("tenant isolation (quote)", c.get("/quotes/1").status_code == 404)
ok("non-admin blocked from admin", b.get("/admin").status_code == 403 and c.post("/admin/2/revoke", data={"csrf": "x"}).status_code in (400, 403))
post(a, "/admin/2/expiry", date=(date.today() - timedelta(days=1)).isoformat())
ok("expired demo blocked server-side", b"expired" in b.get("/leads", follow_redirects=True).data)
post(a, "/admin/2/expiry", date=(date.today() + timedelta(days=3)).isoformat())
post(b, "/login", email="b@x.com", password="bpassword123"); post(a, "/admin/2/revoke")
ok("revoked blocked immediately", b"disabled" in b.get("/leads", follow_redirects=True).data)
