"""Supervisor / orchestrator.

Modes:
  manual      – every step waits for a human "approve" before it runs
  supervised  – scheduled runs; stops only at configured approval gates (writer, alerts)
  autopilot   – continuous scheduled runs; gates auto-approved; full live visibility
Controls: start, pause, resume, skip, rerun step, edit intermediate output, take over, kill switch (+ re-arm).
Every transition is persisted (runs/steps/audit_log) and streamed to the UI as events.
"""
import asyncio
import threading
import time
import traceback
import uuid

from . import db
from .agents import PIPELINE, REGISTRY
from .agents.base import Ctx
from .config import settings


class Killed(Exception):
    pass


class Bus:
    def __init__(self):
        self.subs: set[asyncio.Queue] = set()
        self.loop = None
        self.recent = []

    def publish(self, ev: dict):
        ev.setdefault("ts", time.time())
        self.recent = (self.recent + [ev])[-300:]
        for q in list(self.subs):
            if q.qsize() < 1000:
                q.put_nowait(ev)

    def publish_threadsafe(self, ev):
        if self.loop:
            self.loop.call_soon_threadsafe(self.publish, ev)

    def subscribe(self):
        q = asyncio.Queue()
        self.subs.add(q)
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)


bus = Bus()


class Supervisor:
    def __init__(self):
        self.mode = db.kv_get("mode", settings.default_mode)
        self.status = "idle"  # idle | running | paused | awaiting | killed
        self.killed = bool(db.kv_get("killed", False))
        if self.killed:
            self.status = "killed"
        self.run_id = None
        self.current_step = None
        self.pending = None  # {step_id, kind, phase, ...}
        self._decision = None
        self._decision_evt = asyncio.Event()
        self._pause = threading.Event()
        self._pause.set()  # set == running
        self._task = None
        self.next_run_at = time.time() + 5
        self.interval = settings.interval

    # ---------- state ----------
    def state(self):
        return {"mode": self.mode, "status": self.status, "run_id": self.run_id, "current_step": self.current_step,
                "pending": self.pending, "next_run_at": self.next_run_at if self.mode != "manual" and not self.killed else None,
                "interval": self.interval, "killed": self.killed, "gates": settings.approval_gates,
                "pipeline": [{"name": a.name, "title_fa": a.title_fa, "role_fa": a.role_fa, "gate": a.gate} for a in PIPELINE]}

    def _emit_state(self):
        bus.publish({"type": "state", "state": self.state()})

    def _save_step(self, st):
        with db.tx() as c:
            c.execute("""INSERT INTO steps(id,run_id,agent,idx,status,progress,started,ended,input_summary,output_summary,output,reasoning,error,tokens,edited_by_user)
                VALUES(:id,:run_id,:agent,:idx,:status,:progress,:started,:ended,:input_summary,:output_summary,:output,:reasoning,:error,:tokens,:edited_by_user)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,progress=excluded.progress,started=excluded.started,ended=excluded.ended,
                input_summary=excluded.input_summary,output_summary=excluded.output_summary,output=excluded.output,reasoning=excluded.reasoning,
                error=excluded.error,tokens=excluded.tokens,edited_by_user=excluded.edited_by_user""",
                      {**st, "output": db.jdump(st.get("output")) if not isinstance(st.get("output"), str) else st["output"]})

    def _step_event(self, st, note=None):
        ev = {k: v for k, v in st.items() if k != "output"}
        ev["type"] = "step"
        if note:
            ev["note"] = note
        return ev

    # ---------- controls ----------
    async def control(self, action, actor="user", **kw):
        db.audit(actor, f"control:{action}", self.run_id or "", kw)
        if action == "set_mode":
            m = kw.get("mode")
            if m in ("manual", "supervised", "autopilot"):
                self.mode = m
                db.kv_set("mode", m)
                if m != "manual":
                    self.next_run_at = min(self.next_run_at, time.time() + 3) if self.status == "idle" else self.next_run_at
                if m == "autopilot" and self.pending:  # autopilot resolves open gates
                    self.decide(self.pending["step_id"], "approve", actor="autopilot")
        elif action == "takeover":
            self.mode = "manual"
            db.kv_set("mode", "manual")
            bus.publish({"type": "toast", "level": "info", "msg": "کنترل به کاربر بازگشت (حالت دستی)"})
        elif action == "start":
            if self.killed:
                return {"ok": False, "error": "kill switch active — re-arm first"}
            if self.status == "idle":
                self._task = asyncio.create_task(self.run_pipeline(trigger=f"manual:{actor}"))
        elif action == "pause":
            self._pause.clear()
            if self.status == "running":
                self.status = "paused"
        elif action == "resume":
            self._pause.set()
            if self.status == "paused":
                self.status = "running"
        elif action == "kill":
            self.killed = True
            db.kv_set("killed", True)
            self._pause.set()
            if self.pending:
                self._decision = ("reject", None, actor)
                self._decision_evt.set()
            self.status = "killed"
            bus.publish({"type": "toast", "level": "error", "msg": "کلید توقف اضطراری فعال شد؛ همه اجراها متوقف و زمان‌بند غیرفعال شد"})
        elif action == "arm":
            if kw.get("confirm") != "ARM":
                return {"ok": False, "error": "confirmation required"}
            self.killed = False
            db.kv_set("killed", False)
            self.status = "idle" if not self.run_id else self.status
            self.next_run_at = time.time() + self.interval
        elif action == "set_interval":
            self.interval = max(60, int(kw.get("seconds", self.interval)))
            self.next_run_at = time.time() + self.interval
        self._emit_state()
        return {"ok": True, "state": self.state()}

    def decide(self, step_id, decision, edited_output=None, actor="user"):
        if not self.pending or self.pending["step_id"] != step_id:
            return {"ok": False, "error": "no pending approval for this step"}
        db.audit(actor, f"decision:{decision}", step_id, {"edited": edited_output is not None})
        self._decision = (decision, edited_output, actor)
        self._decision_evt.set()
        return {"ok": True}

    async def _wait_decision(self, st, kind, phase, extra=None):
        self.pending = {"step_id": st["id"], "agent": st["agent"], "kind": kind, "phase": phase, **(extra or {})}
        prev = self.status
        self.status = "awaiting"
        st["status"] = "awaiting_approval"
        self._save_step(st)
        bus.publish(self._step_event(st))
        self._emit_state()
        bus.publish({"type": "toast", "level": "warn", "msg": f"منتظر تأیید: {REGISTRY[st['agent']].title_fa}"})
        if self.mode == "autopilot":
            decision = ("approve", None, "autopilot")
        else:
            self._decision_evt.clear()
            self._decision = None
            await self._decision_evt.wait()
            decision = self._decision
        self.pending = None
        self.status = "killed" if self.killed else (prev if prev != "awaiting" else "running")
        self._emit_state()
        return decision

    # ---------- execution ----------
    def _checkpoint(self):
        if self.killed:
            raise Killed()
        self._pause.wait()
        if self.killed:
            raise Killed()

    async def run_pipeline(self, trigger="schedule", only=None):
        if self.status not in ("idle",) or self.killed:
            return
        run_id = time.strftime("r%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.run_id, self.status = run_id, "running"
        agents = [REGISTRY[only]] if only else PIPELINE
        with db.tx() as c:
            c.execute("INSERT INTO runs(id,mode,trigger,status,started) VALUES(?,?,?,?,?)", (run_id, self.mode, trigger, "running", time.time()))
        bus.publish({"type": "run", "id": run_id, "status": "running", "mode": self.mode, "trigger": trigger, "agents": [a.name for a in agents]})
        steps = []
        for i, A in enumerate(agents):
            st = {"id": f"{run_id}:{A.name}", "run_id": run_id, "agent": A.name, "idx": i, "status": "queued", "progress": 0.0,
                  "started": None, "ended": None, "input_summary": None, "output_summary": None, "output": None,
                  "reasoning": None, "error": None, "tokens": 0, "edited_by_user": 0}
            steps.append(st)
            self._save_step(st)
            bus.publish(self._step_event(st))
        self._emit_state()
        state, final, total_tokens = {}, "succeeded", 0
        loop = asyncio.get_running_loop()
        for st in steps:
            A = REGISTRY[st["agent"]]
            if self.killed:
                st["status"], final = "skipped", "killed"
                self._save_step(st)
                bus.publish(self._step_event(st))
                continue
            # manual mode: pre-step gate
            if self.mode == "manual":
                dec, _, actor = await self._wait_decision(st, "run_step", "pre", {"title_fa": A.title_fa})
                if dec == "skip":
                    st["status"] = "skipped"
                    self._save_step(st)
                    bus.publish(self._step_event(st))
                    continue
                if dec == "reject" or self.killed:
                    st["status"], final = "skipped", ("killed" if self.killed else "aborted")
                    self._save_step(st)
                    bus.publish(self._step_event(st))
                    for rest in steps[st["idx"] + 1:]:
                        rest["status"] = "skipped"
                        self._save_step(rest)
                        bus.publish(self._step_event(rest))
                    break
            self.current_step = st["id"]
            st["status"], st["started"] = "running", time.time()
            self._save_step(st)
            bus.publish(self._step_event(st))
            self._emit_state()
            last_emit = [0.0]

            def progress_cb(p, note, st=st):
                st["progress"] = p
                if time.time() - last_emit[0] > 0.25 or p >= 1:
                    last_emit[0] = time.time()
                    bus.publish_threadsafe(self._step_event(dict(st), note))

            def log_cb(msg, level, st=st):
                bus.publish_threadsafe({"type": "log", "level": level, "agent": st["agent"], "msg": msg, "run_id": run_id})

            ctx = Ctx(run_id, st["id"], progress_cb, self._checkpoint, log_cb, state)
            try:
                res = await asyncio.to_thread(A().run, ctx)
                st.update({"status": res.get("status", "succeeded"), "progress": 1.0, "ended": time.time(),
                           "output_summary": res.get("summary"), "input_summary": res.get("input_summary"),
                           "output": res.get("output"), "reasoning": res.get("reasoning"), "tokens": ctx.tokens})
                total_tokens += ctx.tokens
                appr = res.get("approval")
                if appr and A.gate and self.mode != "manual" and (A.gate in settings.approval_gates or self.mode == "autopilot"):
                    self._save_step(st)
                    dec, edited, actor = await self._wait_decision(st, appr["kind"], "post", appr)
                    self._apply_gate(appr, dec, edited, actor, st)
                    st["status"] = "succeeded" if dec == "approve" else ("skipped" if dec == "skip" else "rejected")
                elif appr:
                    self._apply_gate(appr, "approve" if self.mode == "autopilot" else "hold", None, self.mode, st)
            except Killed:
                st.update({"status": "failed", "error": "killed", "ended": time.time()})
                final = "killed"
            except Exception as e:
                st.update({"status": "failed", "error": f"{e.__class__.__name__}: {e}", "ended": time.time(),
                           "reasoning": traceback.format_exc()[-1500:]})
                final = "partial"
                bus.publish({"type": "toast", "level": "error", "msg": f"خطا در {A.title_fa}: {e}"})
            self._save_step(st)
            bus.publish(self._step_event(st))
        self.current_step = None
        summary = {s["agent"]: s["output_summary"] for s in steps}
        with db.tx() as c:
            c.execute("UPDATE runs SET status=?,ended=?,cost_tokens=?,summary=? WHERE id=?",
                      (final, time.time(), total_tokens, db.jdump(summary), run_id))
        bus.publish({"type": "run", "id": run_id, "status": final})
        try:
            db.checkpoint()
            from . import notify, ops
            notify.background(notify.dispatch_signals)
            await asyncio.to_thread(ops.daily_maintenance)
        except Exception as e:  # noqa: BLE001
            print("post-run maintenance:", e)
        bus.publish({"type": "data_changed"})
        self.status = "killed" if self.killed else "idle"
        self.run_id = None
        self.next_run_at = time.time() + self.interval
        self._emit_state()

    def _apply_gate(self, appr, decision, edited, actor, st):
        kind = appr.get("kind")
        with db.tx() as c:
            if kind == "writer":
                bid = appr["brief_id"]
                if edited is not None:
                    c.execute("UPDATE briefs SET markdown=?,edited_by_user=1 WHERE id=?", (edited, bid))
                    st["edited_by_user"] = 1
                    out = st.get("output") or {}
                    if isinstance(out, dict):
                        out["markdown"] = edited
                status = {"approve": "published", "reject": "rejected", "skip": "draft", "hold": "draft"}[decision]
                c.execute("UPDATE briefs SET status=? WHERE id=?", (status, bid))
            elif kind == "alerts":
                status = {"approve": "approved", "reject": "dismissed", "skip": "pending", "hold": "pending"}[decision]
                ids = appr.get("signal_ids") or []
                if ids:
                    c.execute("UPDATE signals SET status=? WHERE status='pending' AND id IN (" + ",".join("?" for _ in ids) + ")", (status, *ids))
        if decision == "approve":
            bus.publish({"type": "toast", "level": "success", "msg": "بریف منتشر شد" if kind == "writer" else "هشدارهای بحرانی تأیید شد"})
            from . import notify
            if kind == "writer":
                notify.background(notify.dispatch_brief, appr["brief_id"])
            elif kind == "alerts":
                notify.background(notify.dispatch_signals)

    async def rerun(self, agent, actor="user"):
        if agent not in REGISTRY or self.status != "idle" or self.killed:
            return {"ok": False, "error": "busy, killed or unknown agent"}
        db.audit(actor, "rerun", agent)
        self._task = asyncio.create_task(self.run_pipeline(trigger=f"rerun:{agent}", only=agent))
        return {"ok": True}

    async def scheduler(self):
        bus.loop = asyncio.get_running_loop()
        # recover runs interrupted by a restart
        with db.tx() as c:
            c.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            c.execute("UPDATE steps SET status='failed',error='interrupted by restart' WHERE status IN ('running','queued','awaiting_approval')")
        while True:
            await asyncio.sleep(2)
            if self.mode != "manual" and not self.killed and self.status == "idle" and time.time() >= self.next_run_at:
                self._task = asyncio.create_task(self.run_pipeline(trigger=f"schedule:{self.mode}"))


supervisor: Supervisor | None = None


def get():
    global supervisor
    if supervisor is None:
        supervisor = Supervisor()
    return supervisor
