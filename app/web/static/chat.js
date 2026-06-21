/* 共享 AI 投研问答客户端 —— 悬浮窗与独立标签页共用同一份逻辑。
 *
 * 两个入口都打同一套 /api/chat/* + 同一张 DB，会话列表与历史天然一致；
 * 当前会话 id 经 localStorage 同步，两入口可续聊同一对话（只是布局不同）。
 *
 * 用法：const chat = AChat.create({ msgs, text, sendBtn, sesList[, tip, onNavigate, sticky] });
 *   msgs/text/sendBtn/sesList 传各自 DOM 的 element id 字符串。
 *   chat.resume() 载入会话列表并恢复上次对话（无则显示提示语）。
 */
window.AChat = (function () {
  "use strict";
  const SID_KEY = "aic_sid";
  const esc = (s) =>
    (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
    );

  // 紧凑 markdown → html（表格/标题/列表/粗体/引用/代码）
  function md(t) {
    t = esc(t);
    t = t.replace(/(?:^\|.*\|[ \t]*\n?)+/gm, (block) => {
      const rows = block.trim().split("\n").filter((r) => r.trim());
      let out = "<table>", start = 0;
      if (rows[1] && /^\|[\s:|-]+\|$/.test(rows[1].replace(/ /g, ""))) {
        out += "<tr>" + rows[0].split("|").slice(1, -1).map((c) => "<th>" + c.trim() + "</th>").join("") + "</tr>";
        start = 2;
      }
      for (let i = start; i < rows.length; i++)
        out += "<tr>" + rows[i].split("|").slice(1, -1).map((c) => "<td>" + c.trim() + "</td>").join("") + "</tr>";
      return out + "</table>";
    });
    t = t.replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>");
    t = t.replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>");
    t = t.replace(/^[-*] (.*)$/gm, "<li>$1</li>").replace(/(<li>[\s\S]*?<\/li>\n?)+/g, (m) => "<ul>" + m + "</ul>");
    t = t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
    t = t.replace(/^(?!\s*<(?:h\d|ul|li|table|tr|blockquote))(.+)$/gm, "<div>$1</div>");
    return t;
  }

  const DEFAULT_TIP =
    '<div class="aic-tip">问我 A股的事 —— 我会去查 <b style="color:#7c9ef8">真实数据</b>（行情/财报/研报/新闻/你的持仓/板块）再回答。<br>' +
    "例：「中际旭创基本面和风险」「我的持仓有啥要注意的」「CPO板块最近资金怎么样」<br>" +
    '<span style="color:#6b7280">仅供研究 · 不预测涨跌 · 不构成投资建议</span></div>';

  function create(cfg) {
    const $ = (id) => document.getElementById(id);
    const elMsgs = () => $(cfg.msgs);
    const elText = () => $(cfg.text);
    const elSend = () => $(cfg.sendBtn);
    const elSes = () => $(cfg.sesList);
    const TIP = cfg.tip || DEFAULT_TIP;
    const sticky = cfg.sticky !== false;
    let busy = false;
    let sid = sticky ? parseInt(localStorage.getItem(SID_KEY)) || null : null;

    function setSid(v) {
      sid = v || null;
      if (!sticky) return;
      if (sid) localStorage.setItem(SID_KEY, sid);
      else localStorage.removeItem(SID_KEY);
    }
    function scrollB() {
      const m = elMsgs();
      if (m) m.scrollTop = m.scrollHeight;
    }
    function addMsg(role, html) {
      const d = document.createElement("div");
      d.className = "aic-msg " + role;
      d.innerHTML = '<div class="bubble">' + html + "</div>";
      elMsgs().appendChild(d);
      scrollB();
      return d.querySelector(".bubble");
    }
    function showMsgs(list) {
      elMsgs().innerHTML = list && list.length ? "" : TIP;
      (list || []).forEach((m) =>
        addMsg(m.role === "user" ? "user" : "ai", m.role === "user" ? esc(m.content) : '<div class="aic-md">' + md(m.content) + "</div>")
      );
    }

    async function loadSessions() {
      try {
        const j = await (await fetch("/api/chat/sessions")).json();
        elSes().innerHTML =
          (j.sessions || [])
            .map(
              (s) =>
                '<div class="aic-srow' + (s.id === sid ? " cur" : "") + '" data-id="' + s.id + '"><span>' +
                esc(s.title || "新对话") + '</span><span class="del" data-del="' + s.id + '">删</span></div>'
            )
            .join("") || '<div class="aic-srow neu2">暂无历史</div>';
      } catch (e) {}
    }
    async function newSession(reload) {
      const j = await (await fetch("/api/chat/session", { method: "POST" })).json();
      setSid(j.id);
      showMsgs([]);
      if (cfg.onNavigate) cfg.onNavigate();
      if (reload) loadSessions();
    }
    async function openSession(id) {
      setSid(id);
      const j = await (await fetch("/api/chat/session/" + id)).json();
      showMsgs(j.messages);
      if (cfg.onNavigate) cfg.onNavigate();
      loadSessions();
    }
    async function delSession(id) {
      await fetch("/api/chat/session/" + id + "/delete", { method: "POST" });
      if (id === sid) {
        setSid(null);
        showMsgs([]);
      }
      loadSessions();
    }

    async function send() {
      const text = elText().value.trim();
      if (!text || busy) return;
      if (!sid) await newSession(false);
      busy = true;
      elSend().disabled = true;
      elText().value = "";
      if (elMsgs().querySelector(".aic-tip")) elMsgs().innerHTML = "";
      addMsg("user", esc(text));
      const bubble = addMsg("ai", '<span class="aic-status">⏳ 正在思考…</span>');
      let answer = "";
      try {
        const resp = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sid, message: text }),
        });
        const reader = resp.body.getReader(), dec = new TextDecoder();
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const line = buf.slice(0, i);
            buf = buf.slice(i + 2);
            if (!line.startsWith("data: ")) continue;
            let ev;
            try {
              ev = JSON.parse(line.slice(6));
            } catch (e) {
              continue;
            }
            if (ev.type === "status" || ev.type === "thinking") {
              if (!answer) bubble.innerHTML = '<span class="aic-status">' + esc(ev.text) + "</span>";
            } else if (ev.type === "delta") {
              answer += ev.text;
              bubble.innerHTML = '<div class="aic-md">' + md(answer) + "</div>";
              scrollB();
            } else if (ev.type === "error") {
              bubble.innerHTML = '<span style="color:#f87171">' + esc(ev.text) + "</span>";
            }
          }
        }
        if (answer) bubble.innerHTML = '<div class="aic-md">' + md(answer) + "</div>";
        loadSessions();
      } catch (e) {
        bubble.innerHTML = '<span style="color:#f87171">❌ ' + esc(e) + "</span>";
      } finally {
        busy = false;
        elSend().disabled = false;
        scrollB();
      }
    }

    // 共享事件绑定（输入框回车 / 发送 / 会话列表点选+删除）
    if (elSend()) elSend().onclick = send;
    if (elText())
      elText().addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          send();
        }
      });
    if (elSes())
      elSes().onclick = (e) => {
        const del = e.target.closest("[data-del]");
        if (del) {
          e.stopPropagation();
          delSession(+del.dataset.del);
          return;
        }
        const row = e.target.closest("[data-id]");
        if (row) openSession(+row.dataset.id);
      };

    // 恢复：载入会话列表 + 续上次对话（无则显示提示语）
    async function resume() {
      await loadSessions();
      if (sid) {
        try {
          const j = await (await fetch("/api/chat/session/" + sid)).json();
          if (j && j.messages) {
            showMsgs(j.messages);
            return;
          }
        } catch (e) {}
      }
      showMsgs([]);
    }

    return {
      loadSessions, newSession, openSession, delSession, send, showMsgs, resume,
      get sid() { return sid; },
    };
  }

  return { create, md };
})();
