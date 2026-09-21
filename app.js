/**
 * Мряу Mini App — реальные данные из API бота
 *
 * Настройка: в index.html задай
 *   window.MEOW_API_BASE = "https://api.твой-домен.com";
 */

(function () {
  "use strict";

  const API_BASE = (window.MEOW_API_BASE || "").replace(/\/$/, "");

  const RARITIES = {
    common:    { icon: "⚪", name: "Обычная" },
    rare:      { icon: "🔵", name: "Редкая" },
    epic:      { icon: "🟣", name: "Эпическая" },
    mythical:  { icon: "🔴", name: "Мифическая" },
    legendary: { icon: "🟡", name: "Легендарная" },
  };

  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor("#0f0f13");
      tg.setBackgroundColor("#0f0f13");
    } catch (_) {}
  }

  function getInitData() {
    return tg?.initData || "";
  }

  function isTelegram() {
    return !!(tg && tg.initData);
  }

  async function api(path, options = {}) {
    if (!API_BASE || API_BASE.includes("YOUR-API-HOST")) {
      throw new Error(
        'API не настроен. В index.html укажи URL сервера с api.py, например https://api.example.com'
      );
    }
    const initData = getInitData();
    if (!initData) {
      throw new Error(
        "Открой мини-аппку из Telegram (кнопка бота). Без initData API недоступен."
      );
    }

    const url = API_BASE + path;
    let res;
    try {
      res = await fetch(url, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          "X-Telegram-Init-Data": initData,
          ...(options.headers || {}),
        },
      });
    } catch (netErr) {
      throw new Error(
        "Сеть: не удалось связаться с API (" + url + "). Проверь HTTPS и CORS."
      );
    }

    let data = null;
    try {
      data = await res.json();
    } catch (_) {}

    if (!res.ok) {
      if (res.status === 404) {
        throw new Error(
          "404 на " + url + " — это не сервер api.py. MEOW_API_BASE должен указывать на хост, где крутится uvicorn api:app, а не на Vercel/GitHub Pages."
        );
      }
      const msg =
        (data && (data.detail || data.message)) || ("Ошибка " + res.status);
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  let me = null;
  let collection = null;
  let market = null;
  let topData = null;
  let currentFilter = "all";
  let topKind = "coins";

  function fmt(n) {
    return Number(n || 0).toLocaleString("ru-RU").replace(/\s/g, "\u00a0");
  }

  function toast(msg, duration = 2800) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), duration);
  }

  function haptic(type = "light") {
    try {
      if (tg?.HapticFeedback) {
        if (type === "success") tg.HapticFeedback.notificationOccurred("success");
        else if (type === "error") tg.HapticFeedback.notificationOccurred("error");
        else tg.HapticFeedback.impactOccurred(type);
      }
    } catch (_) {}
  }

  function escHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escAttr(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  }

  function rarityEmoji(r) {
    return (
      { common: "🃏", rare: "💠", epic: "🔮", mythical: "🔥", legendary: "⭐" }[
        r
      ] || "🃏"
    );
  }

  // ── Header / Profile ──
  function renderHeader() {
    if (!me) return;
    document.getElementById("userNickname").textContent = me.nickname;
    document.getElementById("userId").textContent = `ID ${me.user_id}`;
    document.getElementById("coinsValue").textContent = fmt(me.coins);
    document.getElementById("gemsValue").textContent = fmt(me.gems);

    const avatarEl = document.getElementById("userAvatar");
    if (me.photo_url) {
      avatarEl.innerHTML = `<img src="${escAttr(me.photo_url)}" alt="" />`;
    } else {
      avatarEl.textContent = "🐱";
    }
  }

  function renderProfile() {
    if (!me) return;
    document.getElementById("profileName").textContent = me.nickname;
    document.getElementById("profileRole").textContent = me.role_display;
    document.getElementById("statCards").textContent = fmt(me.cards_count);
    document.getElementById("statStreak").textContent = me.streak;
    document.getElementById("statDays").textContent = me.days_with_us;

    const parts = (me.gender_display || "— Не задан").split(" ");
    document.querySelector("#genderRow .gender-icon").textContent = parts[0] || "—";
    document.querySelector("#genderRow .gender-text").textContent =
      parts.slice(1).join(" ") || "Не задан";

    const profileAvatar = document.getElementById("profileAvatar");
    if (me.photo_url) {
      profileAvatar.innerHTML = `<img src="${escAttr(me.photo_url)}" alt="" />`;
    } else {
      profileAvatar.textContent = "🐱";
    }

    const hint = document.getElementById("claimHint");
    if (me.can_claim_free) {
      hint.textContent = "Готово ✓";
      hint.style.color = "var(--success)";
    } else {
      const rem = me.cooldown_remaining || 0;
      const h = Math.floor(rem / 3600);
      const m = Math.floor((rem % 3600) / 60);
      hint.textContent = h > 0 ? `через ${h} ч ${m} мин` : `через ${m} мин`;
      hint.style.color = "";
    }
  }

  // ── Collection ──
  function renderRarityFilters() {
    const container = document.getElementById("rarityFilters");
    if (!collection) {
      container.innerHTML = "";
      return;
    }
    const stats = collection.stats || {};
    const totals = collection.rarity_totals || {};

    let html = `<button class="filter-chip ${
      currentFilter === "all" ? "active" : ""
    }" data-rarity="all">Все</button>`;

    for (const [key, info] of Object.entries(RARITIES)) {
      const cnt = stats[key] || 0;
      if (cnt === 0 && currentFilter !== key) continue;
      const tot = totals[key] || 0;
      html += `<button class="filter-chip ${
        currentFilter === key ? "active" : ""
      }" data-rarity="${key}">
        ${info.icon} ${info.name} · ${cnt}${tot ? "/" + tot : ""}
      </button>`;
    }
    container.innerHTML = html;

    container.querySelectorAll(".filter-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        currentFilter = btn.dataset.rarity;
        renderRarityFilters();
        renderCollection();
        haptic("light");
      });
    });
  }

  function renderCollection() {
    const grid = document.getElementById("cardsGrid");
    if (!collection) {
      grid.innerHTML =
        '<div class="empty-state"><div class="empty-icon">⏳</div><p>Загрузка…</p></div>';
      return;
    }

    document.getElementById("collOwned").textContent = collection.owned_unique;
    document.getElementById("collTotal").textContent = collection.total_in_game;

    let items = collection.cards || [];
    if (currentFilter !== "all") {
      items = items.filter((c) => c.rarity === currentFilter);
    }

    if (items.length === 0) {
      grid.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">🃏</div>
          <p>Пока нет карточек</p>
          <p class="muted">Напиши «мряу» в боте, чтобы получить первую</p>
        </div>`;
      return;
    }

    grid.innerHTML = items
      .map((c) => {
        const emoji = rarityEmoji(c.rarity);
        const img =
          c.photo_url && API_BASE
            ? `<img src="${escAttr(API_BASE + c.photo_url)}" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='inline'" /><span class="card-emoji" style="display:none">${emoji}</span>`
            : `<span class="card-emoji">${emoji}</span>`;
        return `
        <div class="card-item" data-id="${c.id}">
          <div class="card-thumb">
            ${img}
            <div class="card-rarity-bar ${c.rarity}"></div>
            ${c.amount > 1 ? `<div class="card-amount">×${c.amount}</div>` : ""}
          </div>
          <div class="card-body">
            <div class="card-name">${escHtml(c.name)}</div>
            <div class="card-meta">${c.rarity_icon} ${escHtml(c.rarity_name)}</div>
          </div>
        </div>`;
      })
      .join("");

    grid.querySelectorAll(".card-item").forEach((el) => {
      el.addEventListener("click", () => openCardModal(+el.dataset.id));
    });
  }

  // ── Market ──
  function renderMarket() {
    if (!market) return;
    document.getElementById("marketGems").textContent = fmt(market.gems);

    const list = document.getElementById("marketList");
    list.innerHTML = (market.rarities || [])
      .map((r) => {
        return `
        <div class="market-item">
          <div class="market-icon ${r.key}">${r.icon}</div>
          <div class="market-info-text">
            <div class="market-name">${escHtml(r.name)}</div>
            <div class="market-desc">${
              r.collected ? "Все собраны" : `Не хватает ${r.missing} шт`
            }</div>
          </div>
          ${
            r.collected
              ? '<span class="market-done">✓ собрано</span>'
              : `<span class="market-price">${r.price} 💎</span>`
          }
        </div>`;
      })
      .join("");
  }

  // ── Top ──
  function renderTop() {
    const list = document.getElementById("topList");
    if (!topData) {
      list.innerHTML = '<div class="empty-state"><p>Загрузка…</p></div>';
      return;
    }

    const unit = topData.unit || "";
    const medals = ["🥇", "🥈", "🥉"];
    const data = topData.top || [];

    list.innerHTML = data
      .map((row, i) => {
        const rankClass =
          i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
        const rank = i < 3 ? medals[i] : `${i + 1}.`;
        return `
        <div class="top-row">
          <div class="top-rank ${rankClass}">${rank}</div>
          <div class="top-nick">${escHtml(row.nickname)}</div>
          <div class="top-value">${fmt(row.value)} ${unit}</div>
        </div>`;
      })
      .join("");

    const meRow = topData.me || {};
    document.getElementById("myRank").innerHTML = `
      📌 Ваше место · <b>#${meRow.rank || "—"}</b><br>
      ${escHtml(meRow.nickname || "")} · <b>${fmt(meRow.value)}</b> ${unit}`;
  }

  // ── Modal ──
  async function openCardModal(cardId) {
    const local =
      collection && (collection.cards || []).find((c) => c.id === cardId);
    if (local) {
      showModal(local);
      return;
    }
    try {
      const card = await api(`/api/card/${cardId}`);
      showModal(card);
    } catch (e) {
      toast(e.message || "Ошибка");
      haptic("error");
    }
  }

  function showModal(card) {
    const modalPhoto = document.getElementById("modalPhoto");
    if (card.photo_url && API_BASE) {
      modalPhoto.innerHTML = `<img src="${escAttr(API_BASE + card.photo_url)}" alt="" onerror="this.parentNode.innerHTML='<span class=\'card-emoji-lg\'>${rarityEmoji(card.rarity)}</span>'" />`;
    } else {
      modalPhoto.innerHTML = `<span class="card-emoji-lg">${rarityEmoji(card.rarity)}</span>`;
    }
    document.getElementById("modalName").textContent = card.name;
    document.getElementById("modalRarity").innerHTML =
      `${card.rarity_icon || ""} ${card.rarity_name || card.rarity}`;
    const parts = [];
    if (card.reward) parts.push(`Награда: +${card.reward} 🪙`);
    if (card.amount) parts.push(`У вас: ×${card.amount}`);
    document.getElementById("modalMeta").textContent = parts.join(" · ");
    document.getElementById("cardModal").classList.add("open");
    haptic("light");
  }

  function closeCardModal() {
    document.getElementById("cardModal").classList.remove("open");
  }

  // ── Load ──
  async function loadMe() {
    me = await api("/api/me");
    renderHeader();
    renderProfile();
  }

  async function loadCollection() {
    collection = await api("/api/collection");
    renderRarityFilters();
    renderCollection();
  }

  async function loadMarket() {
    market = await api("/api/market");
    renderMarket();
  }

  async function loadTop() {
    topData = await api(`/api/top?kind=${topKind}`);
    renderTop();
  }

  async function refreshAll() {
    try {
      await loadMe();
      await Promise.all([loadCollection(), loadMarket(), loadTop()]);
      document.querySelector(".setup-banner")?.remove();
    } catch (e) {
      console.error(e);
      toast(e.message || "Не удалось загрузить данные");
      showSetupHelp(e.message);
    }
  }

  function showSetupHelp(msg) {
    if (document.querySelector(".setup-banner")) return;
    const content = document.getElementById("content");
    const banner = document.createElement("div");
    banner.className = "setup-banner";
    banner.innerHTML = `
      <div class="setup-card">
        <h3>⚠️ Нет данных из бота</h3>
        <p>${escHtml(msg || "API недоступен")}</p>
        <p class="muted">Нужно:</p>
        <ol>
          <li>Запустить <code>api.py</code> на сервере с БД бота</li>
          <li>В <code>index.html</code> указать:
            <pre>window.MEOW_API_BASE = "https://твой-api.ru";</pre>
          </li>
          <li>Открыть мини-аппку <b>из Telegram</b> (кнопка бота)</li>
        </ol>
        <p class="muted">Подробности — в README.md</p>
      </div>`;
    content.prepend(banner);
  }

  async function doExchange(amount) {
    try {
      const res = await api("/api/market/exchange", {
        method: "POST",
        body: JSON.stringify({ amount }),
      });
      toast(`✓ +${amount} 💎`);
      haptic("success");
      me.coins = res.coins;
      me.gems = res.gems;
      renderHeader();
      renderProfile();
      await loadMarket();
    } catch (e) {
      toast(e.message || "Ошибка обмена");
      haptic("error");
    }
  }

  function switchTab(tab) {
    document
      .querySelectorAll(".tab-panel")
      .forEach((p) => p.classList.remove("active"));
    document
      .querySelectorAll(".nav-btn")
      .forEach((b) => b.classList.remove("active"));
    document.querySelector(`.tab-panel[data-tab="${tab}"]`)?.classList.add("active");
    document.querySelector(`.nav-btn[data-tab="${tab}"]`)?.classList.add("active");
    haptic("light");

    if (tab === "collection") {
      if (!collection) loadCollection().catch((e) => toast(e.message));
      else {
        renderRarityFilters();
        renderCollection();
      }
    } else if (tab === "market") {
      if (!market) loadMarket().catch((e) => toast(e.message));
      else renderMarket();
    } else if (tab === "top") {
      if (!topData) loadTop().catch((e) => toast(e.message));
      else renderTop();
    } else if (tab === "profile") {
      renderProfile();
    }
  }

  // Events
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.querySelectorAll(".action-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.dataset.action;
      if (action === "claim") {
        toast("🃏 Напиши «мряу» в чате с ботом, чтобы получить карточку");
        haptic("light");
      } else if (action === "dice") {
        toast("🎲 Напиши «мряу кубик» в чате с ботом");
        haptic("light");
      } else if (action === "market") switchTab("market");
      else if (action === "top") switchTab("top");
    });
  });

  document.querySelectorAll(".top-tab").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document
        .querySelectorAll(".top-tab")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      topKind = btn.dataset.kind;
      haptic("light");
      try {
        await loadTop();
      } catch (e) {
        toast(e.message);
      }
    });
  });

  document.querySelectorAll(".ex-btn").forEach((btn) => {
    btn.addEventListener("click", () => doExchange(+btn.dataset.amount));
  });

  document.getElementById("modalClose").addEventListener("click", closeCardModal);
  document
    .querySelector(".modal-backdrop")
    .addEventListener("click", closeCardModal);

  document.querySelector(".header")?.addEventListener("dblclick", () => {
    refreshAll();
    toast("Обновление…");
  });

  // Start
  if (!API_BASE) {
    showSetupHelp(
      'Не задан адрес API. Добавь в index.html: window.MEOW_API_BASE = "https://твой-api.ru";'
    );
  } else if (!isTelegram()) {
    showSetupHelp(
      "Мини-аппка открыта не из Telegram. Нажми кнопку Web App в боте — появятся твои данные."
    );
  }

  if (API_BASE && isTelegram()) {
    refreshAll();
  }
})();
