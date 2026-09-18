// A minimal Chrome DevTools Protocol client over WebSocket — no dependencies (Node 21+ has a global
// WebSocket). Enough to open a tab, navigate, wait for load, and run our injected filler in it.
export class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = new Map(); }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("CDP connect failed")); });
    const c = new CDP(ws);
    ws.onmessage = (m) => {
      let msg; try { msg = JSON.parse(m.data); } catch (e) { return; }
      if (msg.id && c.pending.has(msg.id)) {
        const { res, rej } = c.pending.get(msg.id); c.pending.delete(msg.id);
        msg.error ? rej(new Error(msg.error.message || "CDP error")) : res(msg.result);
      } else if (msg.method) {
        (c.handlers.get(msg.method) || []).forEach(fn => { try { fn(msg.params, msg.sessionId); } catch (e) {} });
      }
    };
    return c;
  }

  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(`CDP ${method} timed out`)); } }, 60000);
    });
  }
  on(method, fn) { const a = this.handlers.get(method) || []; a.push(fn); this.handlers.set(method, a); }
  close() { try { this.ws.close(); } catch (e) {} }

  // Open a fresh tab at `url`, attach to it, wait for load. Returns the attached sessionId.
  async openTab(url) {
    const { targetId } = await this.send("Target.createTarget", { url: "about:blank" });
    const attached = new Promise(res => this.on("Target.attachedToTarget", (p) => { if (p.targetInfo.targetId === targetId) res(p.sessionId); }));
    await this.send("Target.attachToTarget", { targetId, flatten: true });
    const sessionId = await attached;
    await this.send("Page.enable", {}, sessionId);
    await this.send("Runtime.enable", {}, sessionId);
    const loaded = new Promise(res => { const done = () => res(); this.on("Page.loadEventFired", (_p, sid) => { if (sid === sessionId) done(); }); setTimeout(done, 20000); });
    await this.send("Page.navigate", { url }, sessionId);
    await loaded;
    await new Promise(r => setTimeout(r, 1500));   // let SPA forms render
    return sessionId;
  }

  // Evaluate an EXPRESSION in the page, awaiting a returned promise, and hand back its value.
  async evaluate(sessionId, expression) {
    const r = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "page error");
    return r.result?.value;
  }
}
