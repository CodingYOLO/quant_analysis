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

  // ── 复制工具：复制文字 / 复制长图（html2canvas 懒加载·全站 AI 消息共用）────────
  let _toastT;
  function toast(msg, ok) {
    let t = document.getElementById("aic-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "aic-toast";
      t.style.cssText = "position:fixed;left:50%;bottom:40px;transform:translateX(-50%);z-index:99999;" +
        "background:#1a1d2e;color:#e8e8e8;border:1px solid #3d4270;border-radius:8px;padding:9px 16px;" +
        "font-size:13px;box-shadow:0 6px 22px rgba(0,0,0,.5);opacity:0;transition:opacity .2s;pointer-events:none";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.borderColor = ok === false ? "#6b2a2a" : "#3d4270";
    t.style.opacity = "1";
    clearTimeout(_toastT);
    _toastT = setTimeout(function () { t.style.opacity = "0"; }, 2000);
  }
  function ensureH2C() {
    if (window.html2canvas) return Promise.resolve(window.html2canvas);
    return new Promise(function (res, rej) {
      const s = document.createElement("script");
      s.src = "/static/html2canvas.min.js";
      s.onload = function () { res(window.html2canvas); };
      s.onerror = function () { rej(new Error("html2canvas 加载失败")); };
      document.head.appendChild(s);
    });
  }
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text || "");
      toast("✅ 已复制文字，可粘贴分享");
    } catch (e) {
      toast("复制失败：请手动选中复制", false);
    }
  }
  async function copyImage(bubble, btn) {
    const old = btn.innerHTML;
    btn.innerHTML = "⏳ 生成中…";
    btn.disabled = true;
    let wrap = null;
    try {
      const h2c = await ensureH2C();
      // 克隆整条 .aic-msg(保留 .aic-msg.ai .bubble 样式上下文) 到离屏容器：
      // 脱离 .cpg-msgs 滚动容器(overflow:auto)→ 完整高度全渲染·不被视口截断；并剔除工具条不进图。
      const w = Math.max(bubble.offsetWidth || 640, 360);
      const clone = (bubble.parentNode || bubble).cloneNode(true);
      const tools = clone.querySelector(".aic-tools");
      if (tools) tools.remove();
      const bub = clone.querySelector(".bubble") || clone;
      bub.style.maxWidth = "none";
      bub.style.boxSizing = "border-box";     // 宽含内边距·与屏幕上气泡宽度一致(文字重排不走样)
      bub.style.width = w + "px";
      clone.style.margin = "0";
      wrap = document.createElement("div");
      wrap.style.cssText = "position:fixed;left:-99999px;top:0;z-index:-1;background:#0d0f16;padding:16px;";
      wrap.appendChild(clone);
      document.body.appendChild(wrap);
      const canvas = await h2c(wrap, {
        backgroundColor: "#0d0f16", scale: 2, useCORS: true,
        width: wrap.offsetWidth, height: wrap.offsetHeight,
        windowHeight: wrap.scrollHeight,
      });
      const blob = await new Promise(function (r) { canvas.toBlob(r, "image/png"); });
      let copied = false;
      try {
        if (navigator.clipboard && window.ClipboardItem) {
          await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
          copied = true;
        }
      } catch (e) { copied = false; }
      if (copied) {
        toast("✅ 长图已复制，可直接粘贴分享");
      } else {                                    // 回退：下载 PNG（浏览器不支持复制图片时）
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "AI投研长图.png";
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 3000);
        toast("✅ 已保存长图到下载（本浏览器不支持直接复制图片）");
      }
    } catch (e) {
      toast("长图生成失败：" + (e.message || e), false);
    } finally {
      if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
      btn.innerHTML = old;
      btn.disabled = false;
    }
  }
  function addTools(bubble, rawText) {
    const host = bubble && bubble.parentNode;      // .aic-msg（工具条放气泡外·不进长图）
    if (!host || host.querySelector(".aic-tools")) return;
    const bar = document.createElement("div");
    bar.className = "aic-tools";
    const b1 = document.createElement("button");
    b1.type = "button"; b1.innerHTML = "📋 复制文字";
    b1.onclick = function () { copyText(rawText); };
    const b2 = document.createElement("button");
    b2.type = "button"; b2.innerHTML = "🖼 复制长图";
    b2.onclick = function () { copyImage(bubble, b2); };
    bar.appendChild(b1); bar.appendChild(b2);
    host.appendChild(bar);
  }

  function create(cfg) {
    const $ = (id) => document.getElementById(id);
    const elMsgs = () => $(cfg.msgs);
    const elText = () => $(cfg.text);
    const elSend = () => $(cfg.sendBtn);
    const elSes = () => $(cfg.sesList);
    const TIP = cfg.tip || DEFAULT_TIP;
    const sticky = cfg.sticky !== false;
    let busy = false;
    let nextTask = null;          // 下一条消息的模型档位(pro/flash)·快捷键设·发完即清·默认走后端 pro
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
      (list || []).forEach((m) => {
        const isUser = m.role === "user";
        const bubble = addMsg(isUser ? "user" : "ai",
          isUser ? esc(m.content) : '<div class="aic-md">' + md(m.content) + "</div>");
        if (!isUser && m.content) addTools(bubble, m.content);   // AI 消息加 复制文字/长图
      });
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
      if (j.generating) attach();             // 该会话仍在后台作答 → 接上流
    }
    async function delSession(id) {
      await fetch("/api/chat/session/" + id + "/delete", { method: "POST" });
      if (id === sid) {
        setSid(null);
        showMsgs([]);
      }
      loadSessions();
    }

    // 读取一条 SSE 流并渲染进 bubble，返回最终答案文本（send 与 attach 复用）
    async function pump(resp, bubble) {
      const reader = resp.body.getReader(), dec = new TextDecoder();
      let buf = "", answer = "";
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
          try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
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
      if (answer) {
        bubble.innerHTML = '<div class="aic-md">' + md(answer) + "</div>";
        addTools(bubble, answer);               // 答完加 复制文字/长图
      }
      return answer;
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
      try {
        const task = nextTask; nextTask = null;        // 取本条档位并清空，后续手输消息回落默认
        const resp = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sid, message: text, task: task || undefined }),
        });
        await pump(resp, bubble);
        loadSessions();
      } catch (e) {
        // 连接断了(切页/网络)：后台仍在作答并会落库，回到本页或刷新即可看到完整回答
        bubble.innerHTML = '<span class="aic-status">↻ 显示中断，但 AI 仍在后台作答——回到本页或刷新即可看到完整回答。</span>';
      } finally {
        busy = false;
        elSend().disabled = false;
        scrollB();
      }
    }

    // 切回页面时：若该会话仍在后台生成，接上流把（剩余的）回答显示出来
    async function attach() {
      if (!sid) return;
      const bubble = addMsg("ai", '<span class="aic-status">⏳ AI 还在作答（你刚切走了，这就接着显示）…</span>');
      busy = true;
      if (elSend()) elSend().disabled = true;
      let attached = false;
      try {
        const resp = await fetch("/api/chat/session/" + sid + "/stream");
        const ct = resp.headers.get("content-type") || "";
        if (resp.ok && ct.indexOf("event-stream") >= 0 && resp.body) {
          attached = true;
          await pump(resp, bubble);
          loadSessions();
        }
      } catch (e) {} finally {
        busy = false;
        if (elSend()) elSend().disabled = false;
        scrollB();
      }
      if (!attached) {
        // 没接上在途流（多半已生成完）→ 用库里最终消息重渲染，保证能看到结果
        try {
          const j = await (await fetch("/api/chat/session/" + sid)).json();
          if (j && j.messages) showMsgs(j.messages);
        } catch (e) {}
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
            if (j.generating) attach();        // 切回来时仍在后台作答 → 接上流显示
            return;
          }
        } catch (e) {}
      }
      showMsgs([]);
    }

    return {
      loadSessions, newSession, openSession, delSession, send, showMsgs, resume,
      setTask(t) { nextTask = (t === "pro" || t === "flash") ? t : null; },   // 下一条消息的模型档位
      get sid() { return sid; },
    };
  }

  return { create, md };
})();
