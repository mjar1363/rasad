import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient

from rasad import auth, db, notify, watch
from rasad.api import app
from rasad.config import settings


def test_rbac_setup_login_and_roles(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    with TestClient(app) as c:
        with db.tx() as x:
            x.execute("DELETE FROM users"); x.execute("DELETE FROM sessions"); x.execute("DELETE FROM login_attempts")
        assert c.get("/api/stories").status_code == 401
        st = c.get("/api/auth/status").json()
        assert st["setup_required"] and st["user"] is None
        assert c.post("/api/auth/setup", json={"username": "boss", "password": "short"}).status_code == 400
        r = c.post("/api/auth/setup", json={"username": "boss", "password": "s3cret-pass", "name": "Boss"})
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
        assert c.post("/api/auth/setup", json={"username": "x", "password": "another-pass"}).status_code == 409
        assert c.get("/api/stories").status_code == 200  # cookie session
        assert c.post("/api/users", json={"username": "ana", "password": "analyst-pass", "role": "analyst"}).status_code == 200
        assert c.post("/api/users", json={"username": "vic", "password": "viewer-pass", "role": "viewer"}).status_code == 200
        boss = next(u for u in c.get("/api/users").json() if u["username"] == "boss")
        assert c.post(f"/api/users/{boss['id']}", json={"role": "viewer"}).status_code == 400  # last admin protected
    with TestClient(app) as v:
        assert v.post("/api/auth/login", json={"username": "vic", "password": "wrong-pass"}).status_code == 401
        assert v.post("/api/auth/login", json={"username": "vic", "password": "viewer-pass"}).status_code == 200
        assert v.get("/api/graph").status_code == 200
        assert v.post("/api/watchlists", json={"terms": "x"}).status_code == 403
        assert v.post("/api/control", json={"action": "kill"}).status_code == 403
        assert v.get("/api/users").status_code == 403
    with TestClient(app) as a:
        a.post("/api/auth/login", json={"username": "ana", "password": "analyst-pass"})
        assert a.post("/api/watchlists", json={"name": "t", "terms": "hormuz"}).status_code == 200
        assert a.post("/api/control", json={"action": "kill"}).status_code == 403
        assert a.post("/api/auth/logout").status_code == 200
        assert a.get("/api/stories").status_code == 401
    # throttling after repeated failures
    for _ in range(5):
        try:
            auth.login("ana", "bad", ip="9.9.9.9")
        except PermissionError:
            pass
    try:
        auth.login("ana", "analyst-pass", ip="9.9.9.9")
        assert False, "should be throttled"
    except PermissionError as e:
        assert "۵ دقیقه" in str(e)


class _Hook(BaseHTTPRequestHandler):
    got = []

    def do_POST(self):  # noqa: N802
        _Hook.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_notify_webhook_masking_and_dedup():
    db.init()
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/hook"
    ch = notify.save_channel({"name": "hook", "type": "webhook", "config": {"url": url}, "min_severity": "warning"})
    assert ch["config"]["url"].startswith("•••") and "url" in ch["secrets_set"]
    ch2 = notify.save_channel({"config": {"url": "•••hook"}, "name": "hook2"}, ch["id"])  # masked value keeps the secret
    assert json.loads(db.q1("SELECT config FROM channels WHERE id=?", (ch2["id"],))["config"])["url"] == url
    with db.tx() as c:
        c.execute("""INSERT INTO signals(type,key,label,score,baseline,observed,window,severity,story_ids,explanation,status,created)
                     VALUES('volume_spike','t1','جهش آزمایشی',5,1,5,'6h','warning','[]','{}','new',?)""", (time.time(),))
        c.execute("""INSERT INTO signals(type,key,label,score,baseline,observed,window,severity,story_ids,explanation,status,created)
                     VALUES('volume_spike','t2','بحرانی در انتظار',9,1,9,'6h','critical','[]','{}','pending',?)""", (time.time(),))
    assert notify.dispatch_signals() == 1
    assert notify.dispatch_signals() == 0  # delivered once per (channel, ref)
    assert _Hook.got[-1]["signal"]["label"] == "جهش آزمایشی"
    try:
        notify.save_channel({"type": "telegram", "config": {}})
        assert False
    except ValueError:
        pass
    srv.shutdown()


def test_watchlist_match_and_sources_opml_backup_metrics():
    with TestClient(app) as c:
        with db.tx() as x:
            x.execute("INSERT OR IGNORE INTO sources(id,name,url,lang,status,ownership_group) VALUES('t-src','Test','http://t/rss','en','active','t')")
            for i in range(3):
                x.execute("""INSERT OR REPLACE INTO articles(id,source_id,url,title,lead,lang,published_at,fetched_at,category)
                             VALUES(?, 't-src', ?, ?, 'tanker traffic', 'en', ?, ?, 'shipping')""",
                          (f"wa{i}", f"http://t/{i}", f"Strait of Hormuz closure risk {i}", time.time() - 60, time.time()))
        w = watch.save({"name": "هرمز", "terms": "hormuz, هرمز", "min_articles": 2, "severity": "warning"})
        assert len(watch.hits(w, time.time() - 3600)) == 3
        sigs = [g for g in watch.match() if g["key"].startswith(f"{w['id']}:")]
        assert sigs and sigs[0]["type"] == "watch" and sigs[0]["observed"] == 3
        assert c.get(f"/api/watchlists/{w['id']}/hits").json()[0]["id"].startswith("wa")
        # sources: create without network validation, edit, OPML round-trip
        r = c.post("/api/sources", json={"name": "Example Feed", "url": "https://example.org/feed.xml", "lang": "en", "check": False}).json()
        assert r["source"]["status"] == "pending"
        assert c.post(f"/api/sources/{r['id']}", json={"credibility": 0.9}).status_code == 200
        assert c.post("/api/sources", json={"url": "notaurl", "check": False}).status_code == 400
        opml = c.get("/api/sources/export.opml").text
        assert "example.org/feed.xml" in opml
        imp = c.post("/api/sources/import", json={"opml": opml.replace("example.org/feed.xml", "example.org/other.xml")}).json()
        assert imp["skipped"] >= 1 and len(imp["added"]) >= 1
        # ops
        b = c.post("/api/system/backup").json()
        assert b["size"] > 0 and any(x["file"] == b["file"] for x in c.get("/api/system/backups").json())
        m = c.get("/metrics").text
        assert "rasad_up 1" in m and 'rasad_rows{table="articles"}' in m
        assert c.get("/api/export/articles.csv?hours=1").text.startswith("\ufeffid,source")
        assert "<gexf" in c.get("/api/graph/export.gexf").text
        assert c.post("/api/system/retention", json={"days": 1}).status_code == 400
        assert c.get("/api/system/info").json()["version"] == "0.5.0"


def test_frontend_assets_served():
    with TestClient(app) as c:
        html = c.get("/").text
        for lib in ("sigma.min.js", "graphology.umd.min.js", "graphology-library.min.js"):
            assert lib in html
            assert c.get(f"/static/vendor/{lib}").status_code == 200
        for js in ("graph.js", "admin.js", "auth.js", "core.js", "app.js"):
            assert c.get(f"/static/js/{js}").status_code == 200
