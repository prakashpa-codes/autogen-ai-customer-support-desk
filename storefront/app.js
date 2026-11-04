"use strict";

const API = "/api/v1/public";
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });

// Simple line icons per category (inline SVG keeps the site dependency-free).
const ICONS = {
  bat: '<g transform="rotate(40 12 12)"><rect x="11.1" y="1" width="1.8" height="7.5" rx=".9" fill="currentColor"/><path d="M9 8.6h6l-.5 12a2.5 2.5 0 0 1-5 0z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 10.5v9" stroke="currentColor" stroke-width="1" opacity=".5"/></g>',
  ball: '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 5.5c2 3 2 10 0 13M16 5.5c-2 3-2 10 0 13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-dasharray="1.5 1.5"/>',
  helmet: '<path d="M4 14a8 8 0 0 1 16 0v2H4z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 16v3h8v-3M10 16v3M14 16v3" fill="none" stroke="currentColor" stroke-width="1.4"/>',
  shoe: '<path d="M3 16l1-7 5 2 3 3h6a3 3 0 0 1 3 3H3z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M5 19h1M9 19h1M13 19h1M17 19h1" stroke="currentColor" stroke-width="1.6"/>',
  shirt: '<path d="M8 4l-5 3 2 4 2-1v10h10V10l2 1 2-4-5-3a4 4 0 0 1-8 0z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
  bag: '<rect x="3" y="8" width="18" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 8V6a4 4 0 0 1 8 0v2" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  stumps: '<path d="M7 6v14M12 6v14M17 6v14M6 5h6M12 5h6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  dumbbell: '<path d="M6 8v8M3 10v4M18 8v8M21 10v4M6 12h12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  racket: '<ellipse cx="14" cy="9" rx="5" ry="6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M11 14l-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
};

const $ = (sel) => document.querySelector(sel);
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ------------------------------------------------------------------ catalogue */

let catalogue = { categories: [], products: [] };
let activeCategory = "all";

function productCard(p, icon) {
  const off = p.mrp > p.price ? Math.round((1 - p.price / p.mrp) * 100) : 0;
  return `
    <article class="card">
      <div class="card-art">
        ${p.in_stock ? (off >= 15 ? `<span class="tag">${off}% off</span>` : "") : '<span class="tag out">Out of stock</span>'}
        <svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[icon] || ICONS.ball}</svg>
      </div>
      <div class="card-body">
        <span class="card-brand">${escapeHtml(p.brand)} · ${escapeHtml(p.sku)}</span>
        <h3 class="card-title">${escapeHtml(p.name)}</h3>
        <span class="card-meta">★ ${p.rating} (${p.reviews.toLocaleString("en-IN")}) · ${escapeHtml(p.warranty)}</span>
        <div class="price">
          <strong>${inr.format(p.price)}</strong>
          ${off ? `<s>${inr.format(p.mrp)}</s><span class="off">${off}% off</span>` : ""}
        </div>
        <button class="add-btn" data-sku="${p.sku}" ${p.in_stock ? "" : "disabled"}>${p.in_stock ? "Add to cart" : "Notify me"}</button>
      </div>
    </article>`;
}

function renderProducts() {
  const icons = Object.fromEntries(catalogue.categories.map((c) => [c.id, c.icon]));
  const items = catalogue.products.filter((p) => activeCategory === "all" || p.category === activeCategory);
  $("#product-grid").innerHTML = items.map((p) => productCard(p, icons[p.category])).join("");
  document.querySelectorAll(".chip").forEach((chip) => chip.setAttribute("aria-selected", String(chip.dataset.cat === activeCategory)));
}

function selectCategory(id) {
  activeCategory = id;
  renderProducts();
}

async function loadCatalogue() {
  try {
    const res = await fetch(`${API}/catalogue`);
    catalogue = await res.json();
  } catch {
    $("#product-grid").innerHTML = "<p>Our catalogue is temporarily unavailable. Please try again shortly.</p>";
    return;
  }
  const chips = [{ id: "all", name: "All products" }, ...catalogue.categories];
  $("#category-chips").innerHTML = chips
    .map((c) => `<button class="chip" role="tab" data-cat="${c.id}">${escapeHtml(c.name)}</button>`)
    .join("");
  $("#category-nav").innerHTML = catalogue.categories
    .map((c) => `<a href="#shop" data-cat="${c.id}">${escapeHtml(c.name)}</a>`)
    .join("");
  renderProducts();
}

document.addEventListener("click", (e) => {
  const cat = e.target.closest("[data-cat]");
  if (cat) selectCategory(cat.dataset.cat);
  const add = e.target.closest(".add-btn");
  if (add && !add.disabled) {
    const count = $("#cart-count");
    count.textContent = String(Number(count.textContent) + 1);
    toast("Added to cart");
  }
  if (e.target.closest("[data-open-chat]")) openChat();
});
$("#cart-btn").addEventListener("click", () =>
  toast("Online checkout is paused for maintenance. Chat with us or visit a store to order."),
);

/* ------------------------------------------------------------------ tickets */

async function createTicket(payload) {
  const res = await fetch(`${API}/tickets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new Error(detail || "Something went wrong. Please try again.");
  }
  return body;
}

/** Follow a ticket's progress over Server-Sent Events. Resolves with the final result. */
function followTicket(eventsUrl, onStage) {
  return new Promise((resolve, reject) => {
    const source = new EventSource(eventsUrl);
    source.addEventListener("stage", (e) => onStage(JSON.parse(e.data)));
    source.addEventListener("completed", (e) => {
      source.close();
      resolve(JSON.parse(e.data));
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) reject(new Error("Connection lost."));
    };
  });
}

/* ------------------------------------------------------------------ chat widget */

const chatBody = $("#chat-body");
let chatTicket = null; // one ticket per conversation: {id, token}

function openChat() {
  $("#chat-panel").hidden = false;
  $("#chat-launcher").hidden = true;
  $("#chat-text").focus();
}
$("#chat-close").addEventListener("click", () => {
  $("#chat-panel").hidden = true;
  $("#chat-launcher").hidden = false;
});

function addMessage(kind, html) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.innerHTML = html;
  chatBody.appendChild(el);
  chatBody.scrollTop = chatBody.scrollHeight;
  return el;
}

$("#chat-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#chat-text").value.trim();
  if (text.length < 2) return toast("Please type a message.");
  const button = $("#chat-form .send-btn");
  button.disabled = true;
  $("#chat-text").value = "";
  addMessage("user", escapeHtml(text));
  const status = addMessage("status", "Sending…");
  try {
    const ticket = await createTicket({
      channel: "Chat",
      message: text,
      name: $("#chat-name").value.trim(),
      order_id: $("#chat-order").value.trim(),
      email: $("#chat-email").value.trim(),
      ...(chatTicket ? { ticket_id: chatTicket.id, token: chatTicket.token } : {}),
    });
    chatTicket = { id: ticket.ticket_id, token: ticket.access_token };
    const result = await followTicket(ticket.events_url, (s) => {
      if (s.state === "active") status.textContent = `${s.stage}…`;
    });
    status.remove();
    const tag = result.status === "Resolved"
      ? `<span class="ticket-tag resolved">Answered · ${ticket.ticket_id}</span>`
      : `<span class="ticket-tag escalated">Passed to a specialist · ${ticket.ticket_id}</span>`;
    addMessage("bot", `${escapeHtml(result.reply)}<br>${tag}`);
  } catch (err) {
    status.remove();
    addMessage("bot", escapeHtml(err.message));
  } finally {
    button.disabled = false;
  }
});

/* ------------------------------------------------------------------ ticket form */

$("#ticket-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const data = Object.fromEntries(new FormData(form).entries());
  const out = $("#form-status");
  if (!form.checkValidity()) {
    out.innerHTML = '<span class="err">Please fill in your name, a valid email, a subject and your message.</span>';
    return;
  }
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  out.innerHTML = "Submitting…";
  try {
    const ticket = await createTicket({ channel: "Web Form", ...data });
    const stages = new Map();
    const draw = () => {
      out.innerHTML = `<span class="ok">Ticket ${ticket.ticket_id} received.</span>
        <ul class="progress-list">${[...stages].map(([k, v]) => `<li class="${v}">${escapeHtml(k)}</li>`).join("")}</ul>`;
    };
    draw();
    const result = await followTicket(ticket.events_url, (s) => { stages.set(s.stage, s.state); draw(); });
    out.innerHTML = `<span class="ok">Ticket ${ticket.ticket_id}: our reply has been sent to ${escapeHtml(data.email)}.</span>`
      + (result.status === "Escalated" ? "<br>A support specialist is also reviewing your request." : "");
    form.reset();
  } catch (err) {
    out.innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`;
  } finally {
    button.disabled = false;
  }
});

loadCatalogue();
