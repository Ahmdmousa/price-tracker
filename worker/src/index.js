/**
 * Telegram webhook → instant replies.
 *
 * Answered here immediately (read-only, from data/db.json in the repo):
 *   /help, /list, /watches, /chart <n>, 📈 Chart button, 🎯 Target button (preset choices).
 * Everything that changes data is acknowledged immediately, translated to a text command,
 * saved to data/inbox/<update_id>.json and a GitHub Actions run is started right away.
 * The Python tracker processes the inbox and replies with the result (~30–60 s).
 *
 * Button callback_data (≤ 64 bytes): ch:<sid> tg:<sid> tc:<sid> ts:<sid>:<price> rm:<sid> al:<sid> tr:<sid>
 *
 * Secrets (wrangler secret put): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, GITHUB_TOKEN
 * Vars (wrangler.toml): GITHUB_REPO, DASHBOARD_URL
 */

const STORE_LABELS = { amazon: "Amazon.eg", noon: "Noon", jumia: "Jumia", btech: "B.TECH" };
const URL_RE = /https?:\/\/\S+/;
const QUEUED = {
  "/target": "⏳ Updating…",
  "/remove": "⏳ Updating…",
  "/unwatch": "⏳ Updating…",
  "/watch": "⏳ Searching Amazon.eg and B.TECH…",
  "/alternatives": "⏳ Looking for cheaper listings…",
  "/track": "⏳ Adding it…",
};

const HELP =
  "<b>Price tracker</b>\n" +
  "Send a product link to track it (Amazon.eg, B.TECH; Jumia and Noon are best-effort).\n" +
  "Optional target price after the link: <code>https://… 4500</code>\n\n" +
  "/list — tracked products\n" +
  "/target &lt;n&gt; &lt;price&gt; — set target price (0 clears)\n" +
  "/chart &lt;n&gt; — price history chart\n" +
  "/alternatives &lt;n&gt; — cheaper listings of the same model\n" +
  "/remove &lt;n&gt; — stop tracking\n\n" +
  "/watch iphone 16 plus under 72000 — alert me about any matching offer\n" +
  "/watches — your watches · /unwatch &lt;n&gt;\n\n" +
  "Prices are re-checked every 3 hours, watches every 6 hours.";

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("price-tracker bot is running");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }
    const allowed = String(env.TELEGRAM_CHAT_ID);
    const msg = update.message;
    const cq = update.callback_query;
    let work = null;
    if (msg && typeof msg.text === "string" && String(msg.chat?.id) === allowed) {
      work = handleMessage(update.update_id, msg, env);
    } else if (cq && String(cq.message?.chat?.id) === allowed) {
      work = handleCallback(update.update_id, cq, env);
    }
    if (work) ctx.waitUntil(work.catch((e) => console.error("handler failed", e)));
    // Always 200 quickly so Telegram doesn't retry.
    return new Response("ok");
  },
};

// ------------------------------------------------------------------ messages

async function handleMessage(updateId, msg, env) {
  const chatId = String(msg.chat.id);
  const text = msg.text.trim();

  // Reply to the "✏️ Custom target" prompt
  const prompt = msg.reply_to_message?.text || "";
  const sidMatch = prompt.match(/#([0-9a-f]{8})\b/);
  if (sidMatch && /target/i.test(prompt)) {
    const value = parseFloat(text.replace(/[^\d.]/g, ""));
    if (!isFinite(value)) return send(env, chatId, "Please reply with a number, e.g. 70000");
    return queue(env, chatId, updateId, `/target ${sidMatch[1]} ${value}`, "⏳ Setting target…");
  }

  const parts = text.split(/\s+/);
  const cmd = (parts[0] || "").toLowerCase().split("@")[0];

  if (cmd === "/start" || cmd === "/help") return send(env, chatId, HELP);
  if (cmd === "/list") return send(env, chatId, listText(await loadDb(env)));
  if (cmd === "/watches") return send(env, chatId, watchesText(await loadDb(env)));
  if (cmd === "/chart") {
    const db = await loadDb(env);
    const p = findProduct(db, parts[1] || "");
    if (!p) return send(env, chatId, "Usage: /chart &lt;number from /list&gt;");
    return sendChart(env, chatId, p);
  }
  if (URL_RE.test(text)) return queue(env, chatId, updateId, text, "⏳ Got it, checking the price…");
  if (QUEUED[cmd]) return queue(env, chatId, updateId, text, QUEUED[cmd]);
  return send(env, chatId, HELP);
}

// ------------------------------------------------------------------ buttons

async function handleCallback(updateId, cq, env) {
  const chatId = String(cq.message.chat.id);
  const [kind, sid, extra] = String(cq.data || "").split(":");
  const queuedCmd = {
    ts: `/target ${sid} ${extra}`,
    rm: `/remove ${sid}`,
    al: `/alternatives ${sid}`,
    tr: `/track ${sid}`,
  }[kind];

  if (queuedCmd) {
    await answer(env, cq.id, "⏳ Working on it…");
    return queue(env, chatId, updateId, queuedCmd, null);
  }
  await answer(env, cq.id);

  if (kind === "tc") {
    return tg(env, "sendMessage", {
      chat_id: chatId,
      text: `🎯 Reply with the target price for #${sid}`,
      reply_markup: { force_reply: true, input_field_placeholder: "e.g. 70000" },
    });
  }
  const p = findProduct(await loadDb(env), sid);
  if (!p) return send(env, chatId, "That product isn't tracked anymore.");
  if (kind === "ch") return sendChart(env, chatId, p);
  if (kind === "tg") {
    const now = p.last_price;
    const step = now >= 1000 ? 100 : 10;
    const preset = (pct) => Math.floor((now * (1 - pct)) / step) * step;
    const row = now ? [0.05, 0.1, 0.15].map((pct) => ({
      text: `−${pct * 100}% · ${fmt(preset(pct), "")}`.trim(),
      callback_data: `ts:${p.sid}:${preset(pct)}`,
    })) : [];
    const current = p.target_price ? `\nCurrent target: ${fmt(p.target_price)}` : "";
    return tg(env, "sendMessage", {
      chat_id: chatId,
      parse_mode: "HTML",
      text: `🎯 Target for <b>${esc(short(p.title || p.url, 60))}</b>\nNow ${fmt(now, p.currency)}${current}`,
      reply_markup: {
        inline_keyboard: [
          ...(row.length ? [row] : []),
          [{ text: "✏️ Custom", callback_data: `tc:${p.sid}` }, ...(p.target_price ? [{ text: "✖ Clear", callback_data: `ts:${p.sid}:0` }] : [])],
        ],
      },
    });
  }
}

// ------------------------------------------------------------------ queue → GitHub

async function queue(env, chatId, updateId, text, ack) {
  if (ack) await send(env, chatId, ack);
  try {
    await queueMessage(env, { update_id: updateId, chat_id: chatId, text });
    await triggerRun(env);
  } catch (e) {
    console.error(e);
    await send(env, chatId, "⚠️ Couldn't reach GitHub right now. Please try again in a minute.");
  }
}

function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "price-tracker-worker",
      ...(init.headers || {}),
    },
  });
}

function b64(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

async function queueMessage(env, payload) {
  const body = JSON.stringify({
    message: `Queue Telegram message ${payload.update_id}`,
    content: b64(JSON.stringify(payload)),
  });
  for (let attempt = 0; attempt < 4; attempt++) {
    const r = await gh(env, `/contents/data/inbox/${payload.update_id}.json`, { method: "PUT", body });
    if (r.ok) return;
    if (r.status === 422) return; // already exists → duplicate delivery, already queued
    if (r.status === 409) {        // branch moved under us; retry
      await new Promise((res) => setTimeout(res, 400 * (attempt + 1)));
      continue;
    }
    throw new Error(`queue failed: ${r.status} ${await r.text()}`);
  }
  throw new Error("queue failed after retries");
}

async function triggerRun(env) {
  const r = await gh(env, "/dispatches", { method: "POST", body: JSON.stringify({ event_type: "telegram" }) });
  if (!r.ok) throw new Error(`dispatch failed: ${r.status} ${await r.text()}`);
}

async function loadDb(env) {
  const r = await gh(env, "/contents/data/db.json", { headers: { accept: "application/vnd.github.raw+json" } });
  if (!r.ok) return { products: {}, watches: {} };
  const db = await r.json();
  db.products ||= {};
  db.watches ||= {};
  return db;
}

// ------------------------------------------------------------------ Telegram

async function tg(env, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!r.ok) console.error(method, r.status, await r.text());
  return r;
}

const send = (env, chatId, text) =>
  tg(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true });

const answer = (env, id, text) => tg(env, "answerCallbackQuery", { callback_query_id: id, ...(text ? { text } : {}) });

// ------------------------------------------------------------------ formatting

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (p, c = "EGP") =>
  p == null ? "n/a" : `${Number(p).toLocaleString("en-US", { maximumFractionDigits: 2 })} ${c}`.trim();
const short = (t, n) => (t.length <= n ? t : t.slice(0, n - 1) + "…");

function orderedProducts(db) {
  return Object.entries(db.products)
    .map(([key, p]) => ({ key, ...p }))
    .sort((a, b) => (a.added_at || "").localeCompare(b.added_at || ""));
}

function findProduct(db, ref) {
  const items = orderedProducts(db);
  if (/^\d+$/.test(ref) && +ref >= 1 && +ref <= items.length) return items[+ref - 1];
  return items.find((p) => p.sid === ref || p.key === ref) || null;
}

function listText(db) {
  const items = orderedProducts(db);
  if (!items.length) return "Nothing tracked yet. Send me a product link.";
  return items
    .map((p, i) => {
      const tgt = p.target_price ? ` · target ${fmt(p.target_price)}` : "";
      const stock = p.last_in_stock === false ? " · out of stock" : "";
      const err = p.error_count ? " · ⚠️ error" : "";
      return (
        `${i + 1}. <a href="${esc(p.url)}">${esc(short(p.title || p.url, 55))}</a>\n` +
        `    ${STORE_LABELS[p.store] || p.store} · <b>${fmt(p.last_price, p.currency)}</b>${tgt}${stock}${err}`
      );
    })
    .join("\n");
}

function watchesText(db) {
  const items = Object.values(db.watches).sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
  if (!items.length) return "No watches yet. Try <code>/watch iphone 16 plus under 72000</code>";
  return items
    .map((w, i) => {
      let rng = "";
      if (w.min_price && w.max_price) rng = ` (${fmt(w.min_price)}–${fmt(w.max_price)})`;
      else if (w.max_price) rng = ` (≤ ${fmt(w.max_price)})`;
      const n = Object.keys(w.seen || {}).length;
      return `${i + 1}. “${esc(w.query)}”${rng} · ${n} match${n === 1 ? "" : "es"} seen`;
    })
    .join("\n") + "\n\nRemove one with /unwatch &lt;n&gt;";
}

// ------------------------------------------------------------------ chart

async function sendChart(env, chatId, p) {
  const pts = (p.history || []).filter((h) => h.p != null).map((h) => ({ t: h.t, p: h.p }));
  if (p.last_price != null && p.last_checked) pts.push({ t: p.last_checked, p: p.last_price });
  const title = short(p.title || p.url, 70);
  const summary =
    `<b>${esc(title)}</b>\nNow <b>${fmt(p.last_price, p.currency)}</b> · ` +
    `Low ${fmt(p.lowest_price, p.currency)} · High ${fmt(p.highest_price, p.currency)}` +
    (p.target_price ? ` · Target ${fmt(p.target_price)}` : "");
  if (pts.length < 2) {
    return send(env, chatId, `📈 ${summary}\n\nOnly one price recorded so far. The chart fills in as the price is re-checked.`);
  }
  const step = Math.ceil(pts.length / 60);
  const sample = pts.filter((_, i) => i % step === 0 || i === pts.length - 1);
  const labels = sample.map((x) =>
    new Date(x.t).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "Africa/Cairo" }));
  const datasets = [{
    label: "Price", data: sample.map((x) => x.p), stepped: true,
    borderColor: "#2563eb", borderWidth: 3, pointRadius: 0, fill: false,
  }];
  if (p.target_price) {
    datasets.push({
      label: "Target", data: sample.map(() => p.target_price),
      borderColor: "#16a34a", borderDash: [6, 6], borderWidth: 2, pointRadius: 0, fill: false,
    });
  }
  try {
    const r = await fetch("https://quickchart.io/chart/create", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        version: "4", width: 800, height: 400, backgroundColor: "white",
        chart: {
          type: "line",
          data: { labels, datasets },
          options: {
            plugins: { legend: { display: !!p.target_price }, title: { display: true, text: short(title, 60) } },
            scales: { x: { ticks: { maxTicksLimit: 8 } } },
          },
        },
      }),
    });
    const { url } = await r.json();
    if (!url) throw new Error("no chart url");
    return tg(env, "sendPhoto", { chat_id: chatId, photo: url, caption: summary, parse_mode: "HTML" });
  } catch (e) {
    console.error("chart failed", e);
    const dash = env.DASHBOARD_URL ? `\n<a href="${env.DASHBOARD_URL}">Open the dashboard</a>` : "";
    return send(env, chatId, `📈 ${summary}\n\nCouldn't draw the chart right now.${dash}`);
  }
}
