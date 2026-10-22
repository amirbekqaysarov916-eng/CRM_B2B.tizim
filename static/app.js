
"use strict";

/* ==========================================
   SAGBAN CRM — FRONTEND APPLICATION
   File: static/app.js
   Backend: FastAPI
   ========================================== */

const API = "/api";

const STATUS = {
  NEW: "Yangi",
  ACCEPTED: "Qabul qilingan",
  PREPARING: "Tayyorlanmoqda",
  READY: "Tayyor",
  ON_THE_WAY: "Yo‘lda",
  DELIVERED: "Yetkazilgan",
  CANCELLED: "Bekor qilingan"
};

const STATUS_CLASS = {
  NEW: "status-new",
  ACCEPTED: "status-accepted",
  PREPARING: "status-preparing",
  READY: "status-ready",
  ON_THE_WAY: "status-on-the-way",
  DELIVERED: "status-delivered",
  CANCELLED: "status-cancelled"
};

const NEXT_STATUS = {
  NEW: ["ACCEPTED", "CANCELLED"],
  ACCEPTED: ["PREPARING", "CANCELLED"],
  PREPARING: ["READY", "CANCELLED"],
  READY: ["ON_THE_WAY", "CANCELLED"],
  ON_THE_WAY: ["DELIVERED", "CANCELLED"],
  DELIVERED: [],
  CANCELLED: []
};

const PAGE_TITLES = {
  dashboard: "Dashboard",
  orders: "Buyurtmalar",
  customers: "Mijozlar",
  products: "Mahsulotlar",
  warehouse: "Ombor",
  payments: "To‘lovlar",
  news: "Yangiliklar"
};

const state = {
  admin: null,
  page: "dashboard",
  dashboard: null,
  customers: [],
  products: [],
  orders: [],
  warehouse: [],
  payments: [],
  news: [],
  ws: null,
  wsRetry: null,
  refreshTimer: null,
  authenticated: false
};

/* ==========================================
   DOM HELPERS
   ========================================== */

const $ = (selector, root = document) =>
  root.querySelector(selector);

const $$ = (selector, root = document) =>
  Array.from(root.querySelectorAll(selector));

function text(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = String(value ?? "—");
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[char]);
}

function formatMoney(value) {
  return new Intl.NumberFormat("uz-UZ", {
    maximumFractionDigits: 0
  }).format(Number(value) || 0);
}

function formatKG(grams) {
  const kg = (Number(grams) || 0) / 1000;
  return new Intl.NumberFormat("uz-UZ", {
    maximumFractionDigits: 3
  }).format(kg);
}

function kgInputValue(grams) {
  if (grams === null || grams === undefined) return "";
  return String(Number(grams) / 1000);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";

  return date.toLocaleString("uz-UZ", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function statusBadge(status) {
  return `
    <span class="status-badge ${STATUS_CLASS[status] || ""}">
      ${escapeHTML(STATUS[status] || status)}
    </span>
  `;
}

function emptyRow(columns, message = "Ma’lumot topilmadi") {
  return `
    <tr>
      <td colspan="${columns}" class="empty-state">
        ${escapeHTML(message)}
      </td>
    </tr>
  `;
}

function notify(message, type = "success") {
  const toast = $("#toast");
  if (!toast) return;

  toast.textContent = message;
  toast.className = `toast is-${type}`;
  toast.hidden = false;

  clearTimeout(notify.timer);

  notify.timer = setTimeout(() => {
    toast.hidden = true;
  }, 4000);
}

function setLoading(button, loading) {
  if (!button) return;

  if (loading) {
    button.dataset.originalText = button.textContent;
    button.disabled = true;
    button.textContent = "Kutilmoqda...";
  } else {
    button.disabled = false;
    button.textContent =
      button.dataset.originalText || "Saqlash";
  }
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function getErrorMessage(data, fallback) {
  if (typeof data?.detail === "string") {
    return data.detail;
  }

  if (Array.isArray(data?.detail)) {
    return data.detail
      .map(item => item.msg || "Maydon noto‘g‘ri")
      .join("; ");
  }

  return fallback;
}

/* ==========================================
   API REQUESTS
   ========================================== */

async function request(path, options = {}) {
  const config = {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(options.body !== undefined
        ? { "Content-Type": "application/json" }
        : {})
    }
  };

  if (options.body !== undefined) {
    config.body = JSON.stringify(options.body);
  }

  let response;

  try {
    response = await fetch(`${API}${path}`, config);
  } catch {
    throw new Error(
      "Serverga ulanib bo‘lmadi. Internet yoki serverni tekshiring."
    );
  }

  const data = await response.json().catch(() => ({}));

  if (response.status === 401) {
    if (state.authenticated && path !== "/auth/login") {
      showLogin();
      notify("Sessiya tugadi. Qayta kiring.", "error");
    }

    throw new Error(
      getErrorMessage(data, "Avtorizatsiya talab qilinadi")
    );
  }

  if (!response.ok) {
    throw new Error(
      getErrorMessage(data, `Server xatosi: ${response.status}`)
    );
  }

  return data;
}

/* ==========================================
   AUTHENTICATION
   ========================================== */

function showLogin() {
  state.authenticated = false;
  state.admin = null;

  $("#login-screen").hidden = false;
  $("#app-shell").hidden = true;

  clearTimeout(state.wsRetry);
  clearTimeout(state.refreshTimer);

  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
    state.ws = null;
  }
}

function showApp(admin) {
  state.authenticated = true;
  state.admin = admin;

  $("#login-screen").hidden = true;
  $("#app-shell").hidden = false;

  text("admin-name", admin.username || "Administrator");

  const avatar = $(".avatar");
  if (avatar) {
    avatar.textContent =
      (admin.username || "A").charAt(0).toUpperCase();
  }
}

async function checkSession() {
  try {
    const admin = await request("/auth/me");
    showApp(admin);
    await loadAll();
    connectWebSocket();
  } catch {
    showLogin();
  }
}

async function login(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const error = $("#login-error");

  error.hidden = true;
  setLoading(button, true);

  try {
    const username = $("#login-username").value.trim();
    const password = $("#login-password").value;

    await request("/auth/login", {
      method: "POST",
      body: { username, password }
    });

    $("#login-password").value = "";

    const admin = await request("/auth/me");

    showApp(admin);
    await loadAll();
    connectWebSocket();

    notify("Tizimga muvaffaqiyatli kirdingiz");
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    setLoading(button, false);
  }
}

async function logout() {
  try {
    await request("/auth/logout", {
      method: "POST"
    });
  } catch (err) {
    console.error(err);
  } finally {
    showLogin();
  }
}

/* ==========================================
   NAVIGATION
   ========================================== */

function navigate(page) {
  if (!PAGE_TITLES[page]) return;

  state.page = page;

  $$(".page").forEach(element => {
    const active = element.id === `page-${page}`;
    element.classList.toggle("is-active", active);
    element.hidden = !active;
  });

  $$(".nav-link[data-page]").forEach(button => {
    const active = button.dataset.page === page;

    button.classList.toggle("is-active", active);

    if (active) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });

  text("topbar-title", PAGE_TITLES[page]);

  $("#sidebar").classList.remove("is-open");

  window.scrollTo({
    top: 0,
    behavior: "smooth"
  });
}

/* ==========================================
   DATA LOADING
   ========================================== */

async function loadAll() {
  const endpoints = [
    ["dashboard", "/dashboard"],
    ["customers", "/customers"],
    ["products", "/products"],
    ["orders", "/orders"],
    ["warehouse", "/warehouse"],
    ["payments", "/payments"],
    ["news", "/news"]
  ];

  const results = await Promise.allSettled(
    endpoints.map(([, url]) => request(url))
  );

  results.forEach((result, index) => {
    const key = endpoints[index][0];

    if (result.status === "fulfilled") {
      state[key] = result.value;
    } else {
      console.error(`${key} yuklanmadi:`, result.reason);
    }
  });

  renderAll();

  const failed = results.filter(
    result => result.status === "rejected"
  );

  if (failed.length && state.authenticated) {
    notify(
      `${failed.length} ta bo‘lim yuklanmadi. Serverni tekshiring.`,
      "error"
    );
  }
}

async function refreshData() {
  if (!state.authenticated) return;

  await loadAll();
}

function renderAll() {
  renderDashboard();
  renderCustomers();
  renderProducts();
  renderOrders();
  renderWarehouse();
  renderPayments();
  renderNews();
  updateSelects();
}

/* ==========================================
   DASHBOARD
   ========================================== */

function renderDashboard() {
  const data = state.dashboard;
  if (!data) return;

  const statuses = data.statuses || {};

  text("stat-new-orders", statuses.NEW || 0);
  text("stat-preparing", statuses.PREPARING || 0);
  text("stat-on-way", statuses.ON_THE_WAY || 0);
  text("stat-delivered", statuses.DELIVERED || 0);
  text("stat-sales", formatMoney(data.delivered_sales));
  text("stat-debt", formatMoney(data.total_debt));
  text("stat-customers", data.customers || 0);
  text("stat-products", data.products || 0);

  const total = Object.values(statuses)
    .reduce((sum, count) => sum + Number(count || 0), 0);

  $("#status-breakdown").innerHTML =
    Object.entries(STATUS).map(([key, label]) => {
      const count = Number(statuses[key] || 0);
      const percent = total ? (count / total) * 100 : 0;

      return `
        <div class="status-row">
          <span class="status-row-label">${escapeHTML(label)}</span>
          <div class="status-row-track">
            <div
              class="status-row-fill"
              style="width:${percent}%"
            ></div>
          </div>
          <span class="status-row-value">${count}</span>
        </div>
      `;
    }).join("");

  const recent = data.recent_orders || [];

  $("#dashboard-orders-body").innerHTML = recent.length
    ? recent.map(order => `
      <tr>
        <td><strong>#${order.id}</strong></td>
        <td>${escapeHTML(order.customer)}</td>
        <td>${order.final_total == null
          ? "—"
          : formatMoney(order.final_total) + " so‘m"}</td>
        <td>${statusBadge(order.status)}</td>
        <td>${formatDate(order.created_at)}</td>
        <td>
          <button
            class="table-action"
            data-order-detail="${order.id}"
            type="button"
          >Ko‘rish</button>
        </td>
      </tr>
    `).join("")
    : emptyRow(6);

  const pending = Number(statuses.NEW || 0);
  const badge = $("#nav-orders-count");

  badge.textContent = pending;
  badge.hidden = pending === 0;
}

/* ==========================================
   CUSTOMERS
   ========================================== */

function renderCustomers() {
  const search =
    ($("#customers-search")?.value || "").toLowerCase();

  const customers = state.customers.filter(customer => {
    const value = [
      customer.name,
      customer.phone,
      customer.login_code
    ].join(" ").toLowerCase();

    return value.includes(search);
  });

  $("#customers-table-body").innerHTML = customers.length
    ? customers.map(customer => `
      <tr>
        <td>${customer.id}</td>
        <td><strong>${escapeHTML(customer.name)}</strong></td>
        <td>${escapeHTML(customer.phone || "—")}</td>
        <td><code>${escapeHTML(customer.login_code)}</code></td>
        <td>${formatMoney(customer.credit_limit)} so‘m</td>
        <td>
          <strong>${formatMoney(customer.debt)} so‘m</strong>
        </td>
        <td>
          <span class="status-badge ${
            customer.is_active
              ? "status-active"
              : "status-inactive"
          }">
            ${customer.is_active ? "Faol" : "Nofaol"}
          </span>
        </td>
      </tr>
    `).join("")
    : emptyRow(7);
}

async function createCustomer(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const data = formData(form);

  setLoading(button, true);

  try {
    const result = await request("/customers", {
      method: "POST",
      body: {
        name: data.name.trim(),
        phone: data.phone.trim(),
        address: data.address.trim(),
        credit_limit: Number(data.credit_limit || 0)
      }
    });

    closeModal("customer-modal");
    form.reset();

    await refreshData();

    notify(`Mijoz yaratildi. Login kodi: ${result.login_code}`);
  } catch (err) {
    notify(err.message, "error");
  } finally {
    setLoading(button, false);
  }
}

/* ==========================================
   PRODUCTS
   ========================================== */

function renderProducts() {
  const search =
    ($("#products-search")?.value || "").toLowerCase();

  const products = state.products.filter(product =>
    product.name.toLowerCase().includes(search)
  );

  $("#products-table-body").innerHTML = products.length
    ? products.map(product => {
      const low =
        Number(product.stock_grams) <=
        Number(product.min_stock_grams);

      return `
        <tr>
          <td>${product.id}</td>
          <td><strong>${escapeHTML(product.name)}</strong></td>
          <td>${formatMoney(product.price_per_kg)} so‘m</td>
          <td class="${low ? "stock-low" : "stock-ok"}">
            ${formatKG(product.stock_grams)} kg
          </td>
          <td>${formatKG(product.min_stock_grams)} kg</td>
          <td>
            <span class="status-badge ${
              product.is_active
                ? "status-active"
                : "status-inactive"
            }">
              ${product.is_active ? "Faol" : "Nofaol"}
            </span>
          </td>
        </tr>
      `;
    }).join("")
    : emptyRow(6);
}

async function createProduct(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const data = formData(form);

  setLoading(button, true);

  try {
    await request("/products", {
      method: "POST",
      body: {
        name: data.name.trim(),
        price_per_kg: Number(data.price_per_kg),
        min_stock_grams: Math.round(
          Number(data.min_stock_kg || 0) * 1000
        )
      }
    });

    closeModal("product-modal");
    form.reset();

    await refreshData();

    notify("Mahsulot muvaffaqiyatli qo‘shildi");
  } catch (err) {
    notify(err.message, "error");
  } finally {
    setLoading(button, false);
  }
}

/* ==========================================
   WAREHOUSE
   ========================================== */

function renderWarehouse() {
  const warehouse = state.warehouse;

  $("#warehouse-table-body").innerHTML = warehouse.length
    ? warehouse.map(product => {
      const low =
        Number(product.stock_grams) <=
        Number(product.min_stock_grams);

      return `
        <tr>
          <td><strong>${escapeHTML(product.name)}</strong></td>
          <td class="${low ? "stock-low" : "stock-ok"}">
            ${formatKG(product.stock_grams)} kg
          </td>
          <td>${formatKG(product.min_stock_grams)} kg</td>
          <td>
            <span class="status-badge ${
              low ? "status-new" : "status-active"
            }">
              ${low ? "Zaxira kam" : "Yetarli"}
            </span>
          </td>
          <td>
            <button
              class="table-action"
              type="button"
              data-stock-product="${product.id}"
            >
              + Kirim
            </button>
          </td>
        </tr>
      `;
    }).join("")
    : emptyRow(5);
}

async function createStock(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const data = formData(form);

  setLoading(button, true);

  try {
    await request("/warehouse/in", {
      method: "POST",
      body: {
        product_id: Number(data.product_id),
        quantity_kg: String(data.quantity_kg),
        note: data.note.trim()
      }
    });

    closeModal("stock-modal");
    form.reset();

    await refreshData();

    notify("Mahsulot omborga kirim qilindi");
  } catch (err) {
    notify(err.message, "error");
  } finally {
    setLoading(button, false);
  }
}

/* ==========================================
   ORDERS
   ========================================== */

function estimatedTotal(orderId) {
  const order = state.orders.find(
    item => item.id === Number(orderId)
  );

  return order?.final_total ?? null;
}

function renderOrders() {
  const search =
    ($("#orders-search")?.value || "").toLowerCase();

  const status = $("#orders-status-filter")?.value || "";

  const orders = state.orders.filter(order => {
    const matchesSearch = [
      order.id,
      order.customer_name
    ].join(" ").toLowerCase().includes(search);

    const matchesStatus =
      !status || order.status === status;

    return matchesSearch && matchesStatus;
  });

  $("#orders-table-body").innerHTML = orders.length
    ? orders.map(order => `
      <tr>
        <td><strong>#${order.id}</strong></td>
        <td>${escapeHTML(order.customer_name)}</td>
        <td>—</td>
        <td>
          ${order.final_total == null
            ? "—"
            : formatMoney(order.final_total) + " so‘m"}
        </td>
        <td>${statusBadge(order.status)}</td>
        <td>${formatDate(order.created_at)}</td>
        <td>
          <button
            type="button"
            class="table-action"
            data-order-detail="${order.id}"
          >
            Boshqarish
          </button>
        </td>
      </tr>
    `).join("")
    : emptyRow(7);
}

/* ==========================================
   ORDER FORM
   ========================================== */

function addOrderLine() {
  const template = $("#order-line-template");
  const fragment = template.content.cloneNode(true);

  const select = $(".order-line-product", fragment);

  select.innerHTML = `
    <option value="">Mahsulotni tanlang</option>
    ${state.products
      .filter(product => product.is_active)
      .map(product => `
        <option value="${product.id}">
          ${escapeHTML(product.name)}
          — ${formatMoney(product.price_per_kg)} so‘m/kg
        </option>
      `).join("")}
  `;

  $("#order-lines").appendChild(fragment);
}

async function createOrder(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);

  const customerId = Number($("#order-customer").value);

  const items = $$(".order-line", $("#order-lines"))
    .map(line => ({
      product_id: Number(
        $(".order-line-product", line).value
      ),
      ordered_kg: String(
        $(".order-line-kg", line).value
      )
    }));

  if (!customerId || !items.length) {
    notify("Mijoz va mahsulotlarni tanlang", "error");
    return;
  }

  const productIds = items.map(item => item.product_id);

  if (new Set(productIds).size !== productIds.length) {
    notify("Bir mahsulotni ikki marta tanlamang", "error");
    return;
  }

  if (items.some(item =>
    !item.product_id ||
    !(Number(item.ordered_kg) > 0)
  )) {
    notify("Mahsulot va KG miqdorini tekshiring", "error");
    return;
  }

  setLoading(button, true);

  try {
    const result = await request("/orders", {
      method: "POST",
      body: {
        customer_id: customerId,
        items,
        note: $("#order-note").value.trim()
      }
    });

    closeModal("order-modal");
    form.reset();
    $("#order-lines").innerHTML = "";

    await refreshData();

    navigate("orders");

    notify(`Buyurtma #${result.id} yaratildi`);
  } catch (err) {
    notify(err.message, "error");
  } finally {
    setLoading(button, false);
  }
}

/* ==========================================
   ORDER DETAIL AND ACTUAL KG
   ========================================== */

async function openOrderDetail(orderId) {
  try {
    const data = await request(`/orders/${orderId}`);

    const order = data.order;
    const items = data.items || [];

    const customer = state.customers.find(
      item => item.id === order.customer_id
    );

    const canEditKG = [
      "ACCEPTED",
      "PREPARING"
    ].includes(order.status);

    const orderedTotal = items.reduce(
      (sum, item) =>
        sum + Math.round(
          item.ordered_grams * item.price_per_kg / 1000
        ),
      0
    );

    text(
      "order-detail-title",
      `Buyurtma #${order.id}`
    );

    const rows = items.map(item => `
      <tr>
        <td>${escapeHTML(item.product_name)}</td>
        <td>${formatKG(item.ordered_grams)} kg</td>
        <td>
          ${formatMoney(item.price_per_kg)} so‘m
        </td>
        <td>
          ${canEditKG
            ? `
              <input
                class="actual-kg-input"
                type="number"
                min="0.001"
                step="0.001"
                data-item-id="${item.id}"
                value="${escapeHTML(
                  kgInputValue(item.actual_grams)
                )}"
                aria-label="Real kilogramm"
              >
            `
            : item.actual_grams == null
              ? "Kiritilmagan"
              : `${formatKG(item.actual_grams)} kg`
          }
        </td>
      </tr>
    `).join("");

    const next = NEXT_STATUS[order.status] || [];

    $("#order-detail-content").innerHTML = `
      <div class="row-between">
        <div>
          <strong>Mijoz:</strong>
          ${escapeHTML(customer?.name || `ID: ${order.customer_id}`)}
        </div>
        ${statusBadge(order.status)}
      </div>

      <p>
        <strong>Buyurtma sanasi:</strong>
        ${formatDate(order.created_at)}
      </p>

      <p>
        <strong>Izoh:</strong>
        ${escapeHTML(order.note || "Yo‘q")}
      </p>

      <h3>Mahsulotlar</h3>

      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Mahsulot</th>
              <th>Buyurtma KG</th>
              <th>Narx/kg</th>
              <th>Real KG</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>

      <div style="margin-top:18px">
        <p>
          <strong>Taxminiy summa:</strong>
          ${formatMoney(orderedTotal)} so‘m
        </p>

        <p>
          <strong>Yakuniy summa:</strong>
          ${order.final_total == null
            ? "Hali hisoblanmagan"
            : `${formatMoney(order.final_total)} so‘m`}
        </p>
      </div>

      ${canEditKG ? `
        <div style="margin-top:18px">
          <button
            class="btn btn-outline"
            type="button"
            data-save-actual="${order.id}"
          >
            Real KG ni saqlash
          </button>
        </div>
      ` : ""}

      ${next.length ? `
        <div style="margin-top:24px">
          <h3>Buyurtma statusi</h3>
          <div class="row-between" style="flex-wrap:wrap">
            ${next.map(status => `
              <button
                type="button"
                class="btn ${
                  status === "CANCELLED"
                    ? "btn-ghost"
                    : "btn-primary"
                }"
                data-change-status="${order.id}"
                data-next-status="${status}"
              >
                ${status === "CANCELLED"
                  ? "Bekor qilish"
                  : STATUS[status]}
              </button>
            `).join("")}
          </div>
        </div>
      ` : ""}
    `;

    openModal("order-detail-modal");

  } catch (err) {
    notify(err.message, "error");
  }
}

async function saveActualKG(orderId) {
  const inputs = $$(".actual-kg-input");

  const items = inputs.map(input => ({
    item_id: Number(input.dataset.itemId),
    actual_kg: String(input.value)
  }));

  if (!items.length || items.some(item =>
    !(Number(item.actual_kg) > 0)
  )) {
    notify(
      "Barcha mahsulotlar uchun real KG kiriting",
      "error"
    );
    return;
  }

  try {
    await request(`/orders/${orderId}/actual`, {
      method: "PUT",
      body: { items }
    });

    notify("Real KG saqlandi");

    await refreshData();
    await openOrderDetail(orderId);

  } catch (err) {
    notify(err.message, "error");
  }
}

async function changeOrderStatus(orderId, status) {
  if (status === "CANCELLED") {
    const confirmed = confirm(
      `#${orderId} buyurtmani bekor qilasizmi?`
    );

    if (!confirmed) return;
  }

  try {
    await request(`/orders/${orderId}/status`, {
      method: "POST",
      body: { status }
    });

    notify(
      `Buyurtma statusi: ${STATUS[status]}`
    );

    await refreshData();
    await openOrderDetail(orderId);

  } catch (err) {
    notify(err.message, "error");
  }
}

/* ==========================================
   PAYMENTS
   ========================================== */

function renderPayments() {
  const payments = state.payments;

  $("#payments-table-body").innerHTML = payments.length
    ? payments.map(payment => `
      <tr>
        <td>${payment.id}</td>
        <td>
          ${escapeHTML(
            state.customers.find(
              customer => customer.id === payment.customer_id
            )?.name || `ID: ${payment.customer_id}`
          )}
        </td>
        <td>
          <strong>${formatMoney(payment.amount)} so‘m</strong>
        </td>
        <td>${escapeHTML(payment.note || "—")}</td>
        <td>${formatDate(payment.created_at)}</td>
      </tr>
    `).join("")
    : emptyRow(5);
}

async function createPayment(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const data = formData(form);

  setLoading(button, true);

  try {
    await request("/payments", {
      method: "POST",
      body: {
        customer_id: Number(data.customer_id),
        amount: Number(data.amount),
        note: data.note.trim()
      }
    });

    closeModal("payment-modal");
    form.reset();

    await refreshData();

    notify("To‘lov muvaffaqiyatli saqlandi");

  } catch (err) {
    notify(err.message, "error");
  } finally {
    setLoading(button, false);
  }
}

/* ==========================================
   NEWS
   ========================================== */

function renderNews() {
  const news = state.news;

  $("#news-list").innerHTML = news.length
    ? news.map(item => `
      <article class="news-card">
        <h3>${escapeHTML(item.title)}</h3>
        <p>${escapeHTML(item.message)}</p>
        <time>${formatDate(item.created_at)}</time>
      </article>
    `).join("")
    : `<p class="empty-state">Yangiliklar mavjud emas</p>`;
}

async function createNews(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const data = formData(form);

  setLoading(button, true);

  try {
    await request("/news", {
      method: "POST",
      body: {
        title: data.title.trim(),
        message: data.message.trim()
      }
    });

    closeModal("news-modal");
    form.reset();

    await refreshData();

    notify("Yangilik e’lon qilindi");

  } catch (err) {
    notify(err.message, "error");
  } finally {
    setLoading(button, false);
  }
}

/* ==========================================
   SELECT OPTIONS
   ========================================== */

function updateSelects() {
  const activeCustomers = state.customers.filter(
    customer => customer.is_active
  );

  const activeProducts = state.products.filter(
    product => product.is_active
  );

  const customerOptions = `
    <option value="">Mijozni tanlang</option>
    ${activeCustomers.map(customer => `
      <option value="${customer.id}">
        ${escapeHTML(customer.name)}
      </option>
    `).join("")}
  `;

  const productOptions = `
    <option value="">Mahsulotni tanlang</option>
    ${activeProducts.map(product => `
      <option value="${product.id}">
        ${escapeHTML(product.name)}
      </option>
    `).join("")}
  `;

  [
    "#order-customer",
    "#payment-customer"
  ].forEach(selector => {
    const select = $(selector);
    if (!select) return;

    const previous = select.value;
    select.innerHTML = customerOptions;

    if ([...select.options].some(
      option => option.value === previous
    )) {
      select.value = previous;
    }
  });

  const stockSelect = $("#stock-product");

  if (stockSelect) {
    const previous = stockSelect.value;
    stockSelect.innerHTML = productOptions;

    if ([...stockSelect.options].some(
      option => option.value === previous
    )) {
      stockSelect.value = previous;
    }
  }
}

/* ==========================================
   MODAL MANAGEMENT
   ========================================== */

function openModal(id) {
  const modal = document.getElementById(id);

  if (!modal) return;

  if (id === "order-modal") {
    updateSelects();

    if (!$("#order-lines").children.length) {
      addOrderLine();
    }
  }

  if (!modal.open) {
    modal.showModal();
  }
}

function closeModal(id) {
  const modal = document.getElementById(id);

  if (modal?.open) {
    modal.close();
  }
}

/* ==========================================
   REAL-TIME WEBSOCKET
   ========================================== */

function setConnectionStatus(online) {
  const indicator = $("#connection-indicator");

  if (!indicator) return;

  indicator.classList.toggle("is-online", online);
  indicator.classList.toggle("is-offline", !online);

  indicator.textContent = online
    ? "● Real-time ulangan"
    : "● Real-time uzilgan";
}

function connectWebSocket() {
  if (!state.authenticated) return;

  clearTimeout(state.wsRetry);

  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
  }

  const protocol =
    location.protocol === "https:" ? "wss:" : "ws:";

  const url = `${protocol}//${location.host}/ws`;

  const socket = new WebSocket(url);

  state.ws = socket;

  socket.onopen = () => {
    setConnectionStatus(true);
  };

  socket.onmessage = event => {
    try {
      const message = JSON.parse(event.data);

      console.log("SAGBAN event:", message.event);

      clearTimeout(state.refreshTimer);

      state.refreshTimer = setTimeout(() => {
        refreshData();
      }, 500);

    } catch (err) {
      console.error("WebSocket xabar xatosi:", err);
    }
  };

  socket.onerror = () => {
    setConnectionStatus(false);
  };

  socket.onclose = () => {
    if (state.ws !== socket) return;

    setConnectionStatus(false);

    if (state.authenticated) {
      state.wsRetry = setTimeout(
        connectWebSocket,
        5000
      );
    }
  };
}

/* ==========================================
   EVENT HANDLERS
   ========================================== */

function registerEvents() {
  $("#login-form").addEventListener("submit", login);

  $("#logout-btn").addEventListener("click", logout);

  $("#refresh-btn").addEventListener(
    "click",
    refreshData
  );

  $("#sidebar-open").addEventListener("click", () => {
    $("#sidebar").classList.add("is-open");
  });

  $("#sidebar-close").addEventListener("click", () => {
    $("#sidebar").classList.remove("is-open");
  });

  $("#customer-form").addEventListener(
    "submit",
    createCustomer
  );

  $("#product-form").addEventListener(
    "submit",
    createProduct
  );

  $("#stock-form").addEventListener(
    "submit",
    createStock
  );

  $("#order-form").addEventListener(
    "submit",
    createOrder
  );

  $("#payment-form").addEventListener(
    "submit",
    createPayment
  );

  $("#news-form").addEventListener(
    "submit",
    createNews
  );

  $("#customers-search").addEventListener(
    "input",
    renderCustomers
  );

  $("#products-search").addEventListener(
    "input",
    renderProducts
  );

  $("#orders-search").addEventListener(
    "input",
    renderOrders
  );

  $("#orders-status-filter").addEventListener(
    "change",
    renderOrders
  );

  $("#add-order-line").addEventListener(
    "click",
    addOrderLine
  );

  document.addEventListener("click", async event => {
    const target = event.target;

    const pageButton = target.closest("[data-page]");

    if (pageButton) {
      navigate(pageButton.dataset.page);
      return;
    }

    const modalButton =
      target.closest("[data-open-modal]");

    if (modalButton) {
      openModal(modalButton.dataset.openModal);
      return;
    }

    const closeButton =
      target.closest("[data-close-modal]");

    if (closeButton) {
      const modal = closeButton.closest("dialog");

      if (modal) closeModal(modal.id);
      return;
    }

    const removeLine =
      target.closest(".remove-order-line");

    if (removeLine) {
      removeLine.closest(".order-line").remove();
      return;
    }

    const stockButton =
      target.closest("[data-stock-product]");

    if (stockButton) {
      openModal("stock-modal");

      $("#stock-product").value =
        stockButton.dataset.stockProduct;

      return;
    }

    const orderButton =
      target.closest("[data-order-detail]");

    if (orderButton) {
      await openOrderDetail(
        Number(orderButton.dataset.orderDetail)
      );
      return;
    }

    const actualButton =
      target.closest("[data-save-actual]");

    if (actualButton) {
      await saveActualKG(
        Number(actualButton.dataset.saveActual)
      );
      return;
    }

    const statusButton =
      target.closest("[data-change-status]");

    if (statusButton) {
      await changeOrderStatus(
        Number(statusButton.dataset.changeStatus),
        statusButton.dataset.nextStatus
      );
    }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      $("#sidebar").classList.remove("is-open");
    }
  });
}

/* ==========================================
   APPLICATION INITIALIZATION
   ========================================== */

async function initializeApp() {
  const today = new Date();

  text(
    "current-date",
    today.toLocaleDateString("uz-UZ", {
      day: "numeric",
      month: "long",
      year: "numeric"
    })
  );

  text("footer-year", today.getFullYear());

  registerEvents();

  navigate("dashboard");

  await checkSession();
}

document.addEventListener(
  "DOMContentLoaded",
  initializeApp
);
