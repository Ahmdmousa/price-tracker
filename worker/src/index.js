/**
 * Telegram webhook → instant replies.
 *
 * - /help and /list are answered immediately here.
 * - Everything else (links, /target, /remove) is acknowledged immediately, saved to
 *   data/inbox/<update_id>.json in the repo, and a GitHub Actions run is started right away.
 *   The Python tracker processes the inbox and replies with the result (~30–60 s).
 *
 * Secrets (wrangler secret put): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, GITHUB_TOKEN
 * Var (wrangler.toml): GITHUB_REPO = "owner/repo"
 */

const STORE_LABELS = { amazon: "Amazon.eg", noon: "Noon", jumia: "Jumia", btech: "B.TECH" };
const URL_RE = /https?:\/\/\S+/;

const HELP =
  "<b>Price tracker</b>\n" +
  "Send a product link to track it (Amazon.eg, B.TECH; Jumia and Noon are best-effort).\n" +
  "Optional target price after the link: <code>https://… 4500</code>\n\n" +
  "/list — tracked products\n" +
  "/target &lt;n&gt; &lt;price&gt; — set target price (0 clears)\n" +
  "/remove &lt;n&gt; — stop tracking\n" +
  "/help — this message\n\n" +
  "Prices are re-checked every 3 hours. You get alerts on drops, targets and restocks.";

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
    const msg = update.message;
    if (msg && typeof msg.text === "string" && String(msg.chat?.id) === String(env.TELEGRAM_CHAT_ID)) {
      ctx.waitUntil(handle(update.update_id, msg, env).catch((e) => console.error("handle failed", e)));
    }
    // Always 200 quickly so Telegram doesn't retry.
    return new Response("ok");
  },
};

async function handle(updateId, msg, env) {
  const chatId = String(msg.chat.id);
  const text = msg.text.trim();
  const cmd = (text.split(/\s+/)[0] || "").toLowerCase().split("@")[0];

  if (cmd === "/start" || cmd === "/help") return send(env, chatId, HELP);
  if (cmd === "/list") return send(env, chatId, await listText(env));

  const isLink = URL_RE.test(text);
  if (!isLink && cmd !== "/target" && cmd !== "/remove") return send(env, chatId, HELP);

  await send(env, chatId, isLink ? "⏳ Got it, checking the price…" : "⏳ Updating…");
  try {
    await queueMessage(env, { update_id: updateId, chat_id: chatId, text, date: msg.date });
    await triggerRun(env);
  } catch (e) {
    console.error(e);
    await send(env, chatId, "⚠️ Couldn't reach GitHub right now. Please send it again in a minute.");
  }
}

// ------------------------------------------------------------------ Telegram

async function send(env, chatId, text) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
  if (!r.ok) console.error("sendMessage", r.status, await r.text());
}

// ------------------------------------------------------------------ GitHub

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
    if (r.status === 422) return; // file already exists → duplicate delivery, already queued
    if (r.status === 409) {        // branch moved under us; retry
      await new Promise((res) => setTimeout(res, 400 * (attempt + 1)));
      continue;
    }
    throw new Error(`queue failed: ${r.status} ${await r.text()}`);
  }
  throw new Error("queue failed after retries");
}

async function triggerRun(env) {
  const r = await gh(env, "/dispatches", {
    method: "POST",
    body: JSON.stringify({ event_type: "telegram" }),
  });
  if (!r.ok) throw new Error(`dispatch failed: ${r.status} ${await r.text()}`);
}

// ------------------------------------------------------------------ /list

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (p, c = "EGP") =>
  p == null ? "n/a" : `${Number(p).toLocaleString("en-US", { maximumFractionDigits: 2 })} ${c}`;
const short = (t, n) => (t.length <= n ? t : t.slice(0, n - 1) + "…");

async function listText(env) {
  const r = await gh(env, "/contents/data/db.json", { headers: { accept: "application/vnd.github.raw+json" } });
  if (r.status === 404) return "Nothing tracked yet. Send me a product link.";
  if (!r.ok) return "⚠️ Couldn't load the list right now.";
  const db = await r.json();
  const items = Object.values(db.products || {}).sort((a, b) => (a.added_at || "").localeCompare(b.added_at || ""));
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
