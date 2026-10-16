(() => {
  'use strict';

  const SESSION_KEY = 'paylab.session';
  const CART_KEY = 'paylab.cart';
  const POLL_INTERVAL_MS = 500;
  const POLL_GIVE_UP_MS = 60000;
  const FINAL_STATUSES = ['paid', 'payment_failed', 'partially_refunded', 'refunded'];
  const PAGE_SIZE = 10;

  const main = document.getElementById('main');
  let pollTimer = null;
  let viewToken = 0;

  // ---------- storage ----------
  function readJson(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return fallback;
    }
  }
  function writeJson(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* storage unavailable */ }
  }

  function getSession() { return readJson(SESSION_KEY, null); }
  function setSession(s) {
    if (s) writeJson(SESSION_KEY, s); else localStorage.removeItem(SESSION_KEY);
    renderHeader();
  }

  function getCart() { return readJson(CART_KEY, []); }
  function setCart(lines) { writeJson(CART_KEY, lines); renderCartBadge(); }
  function cartCount() { return getCart().reduce((n, l) => n + l.qty, 0); }

  // ---------- helpers ----------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function money(value) { return '$' + value; }

  function centsOf(value) { return Math.round(parseFloat(value) * 100); }
  function fromCents(cents) { return (cents / 100).toFixed(2); }

  function formatDate(iso) {
    const d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString('en-CA');
  }

  const STATUS_LABELS = {
    pending: 'Awaiting payment',
    processing: 'Processing payment',
    paid: 'Paid',
    payment_failed: 'Payment failed',
    partially_refunded: 'Partially refunded',
    refunded: 'Refunded',
  };
  function statusLabel(s) { return STATUS_LABELS[s] || s; }

  function setBanner(el, kind, text) {
    el.className = 'banner' + (kind ? ' banner--' + kind : '');
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = text || '';
  }

  function banner(attrs) {
    return h('div', Object.assign({ class: 'banner', role: 'status' }, attrs || {}));
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function navigate(hash) {
    if (location.hash === hash) render(); else location.hash = hash;
  }

  function setTitle(text) {
    document.title = text + ' · PayLab Store';
  }

  // ---------- API ----------
  async function api(method, path, body, extraHeaders) {
    const headers = Object.assign({ Accept: 'application/json' }, extraHeaders || {});
    const session = getSession();
    if (session) headers.Authorization = 'Bearer ' + session.token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let res;
    try {
      res = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      return { ok: false, status: 0, data: null };
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401 && session && path !== '/auth/token') {
      setSession(null);
      sessionStorage.setItem('paylab.flash', 'Your session has ended. Please sign in again.');
      navigate('#/login');
    }
    return { ok: res.ok, status: res.status, data };
  }

  function errorMessage(result, fallback) {
    if (result.status === 0) return 'We could not reach the store. Check your connection and try again.';
    if (result.status === 403) return 'You do not have permission to do that.';
    if (result.status === 404) return 'We could not find that order.';
    if (result.data && result.data.message) return result.data.message;
    return fallback;
  }

  // ---------- header ----------
  function renderCartBadge() {
    const count = cartCount();
    const badge = document.querySelector('[data-testid="cart-badge"]');
    if (badge) badge.textContent = String(count);
    const label = document.getElementById('cart-count-label');
    if (label) label.textContent = '(' + count + (count === 1 ? ' item)' : ' items)');
  }

  function renderHeader() {
    const session = getSession();
    document.querySelectorAll('[data-auth="user"]').forEach((el) => { el.hidden = !session; });
    document.querySelectorAll('[data-auth="admin"]').forEach((el) => {
      el.hidden = !(session && session.role === 'admin');
    });
    document.getElementById('session-user').textContent = session ? 'Signed in as ' + session.username : '';
    document.getElementById('login-link').hidden = Boolean(session);
    document.getElementById('logout-button').hidden = !session;
    renderCartBadge();
  }

  // ---------- views ----------
  function viewLogin() {
    setTitle('Sign in');
    const flash = sessionStorage.getItem('paylab.flash');
    sessionStorage.removeItem('paylab.flash');
    const msg = banner();
    if (flash) setBanner(msg, 'info', flash);
    const username = h('input', { id: 'login-username', name: 'username', type: 'text', autocomplete: 'username', required: true });
    const password = h('input', { id: 'login-password', name: 'password', type: 'password', autocomplete: 'current-password', required: true });
    const submit = h('button', { type: 'submit' }, 'Sign in');
    const form = h('form', { 'aria-labelledby': 'login-heading', novalidate: true },
      h('div', { class: 'field' }, h('label', { for: 'login-username' }, 'Username'), username),
      h('div', { class: 'field' }, h('label', { for: 'login-password' }, 'Password'), password),
      submit);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (!username.value.trim() || !password.value) {
        setBanner(msg, 'error', 'Enter your username and password.');
        return;
      }
      submit.disabled = true;
      const res = await api('POST', '/auth/token', { username: username.value.trim(), password: password.value });
      submit.disabled = false;
      if (res.ok) {
        setSession({ token: res.data.access_token, role: res.data.role, username: username.value.trim() });
        const next = sessionStorage.getItem('paylab.next') || '#/products';
        sessionStorage.removeItem('paylab.next');
        navigate(next);
      } else if (res.status === 401) {
        setBanner(msg, 'error', 'Incorrect username or password.');
      } else {
        setBanner(msg, 'error', errorMessage(res, 'Sign in failed. Please try again.'));
      }
    });
    main.replaceChildren(
      h('h1', { id: 'login-heading' }, 'Sign in'),
      h('section', { class: 'card' }, msg, form));
  }

  async function viewProducts(params) {
    setTitle('Products');
    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
    const msg = banner();
    main.replaceChildren(h('h1', {}, 'Products'), msg, h('p', {}, 'Loading products…'));
    const res = await api('GET', '/products?page=' + page + '&limit=' + PAGE_SIZE);
    if (!res.ok) {
      main.replaceChildren(h('h1', {}, 'Products'), msg);
      setBanner(msg, 'error', errorMessage(res, 'Products could not be loaded.'));
      return;
    }
    const data = res.data;
    const pages = Math.max(1, Math.ceil(data.total_items / data.limit));
    const rows = data.items.map((p) => h('tr', {},
      h('td', {}, p.name),
      h('td', { class: 'muted' }, p.sku),
      h('td', { class: 'num' }, money(p.unit_price)),
      h('td', {}, h('button', {
        type: 'button',
        'aria-label': 'Add ' + p.name + ' to cart',
        onclick: () => {
          const lines = getCart();
          const line = lines.find((l) => l.sku === p.sku);
          if (line) line.qty += 1; else lines.push({ sku: p.sku, name: p.name, unit_price: p.unit_price, qty: 1 });
          setCart(lines);
          setBanner(msg, 'success', p.name + ' added to your cart.');
        },
      }, 'Add to cart'))));
    main.replaceChildren(
      h('h1', {}, 'Products'),
      msg,
      h('table', {},
        h('caption', { class: 'visually-hidden' }, 'Product catalogue, page ' + page + ' of ' + pages),
        h('thead', {}, h('tr', {},
          h('th', { scope: 'col' }, 'Product'), h('th', { scope: 'col' }, 'SKU'),
          h('th', { scope: 'col', class: 'num' }, 'Price'), h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
        h('tbody', {}, rows)),
      pagination('#/products', page, pages));
  }

  function pagination(base, page, pages) {
    return h('nav', { class: 'pagination', 'aria-label': 'Pagination' },
      page > 1 ? h('a', { href: base + '?page=' + (page - 1), rel: 'prev' }, 'Previous page') : h('span', { class: 'muted' }, 'Previous page'),
      h('span', { 'aria-current': 'page' }, 'Page ' + page + ' of ' + pages),
      page < pages ? h('a', { href: base + '?page=' + (page + 1), rel: 'next' }, 'Next page') : h('span', { class: 'muted' }, 'Next page'));
  }

  function viewCart() {
    setTitle('Cart');
    const msg = banner();
    const lines = getCart();
    if (lines.length === 0) {
      main.replaceChildren(h('h1', {}, 'Your cart'),
        h('p', {}, 'Your cart is empty. ', h('a', { href: '#/products' }, 'Browse products')));
      return;
    }
    const subtotalCents = lines.reduce((sum, l) => sum + centsOf(l.unit_price) * l.qty, 0);
    const rows = lines.map((l) => {
      const inputId = 'qty-' + l.sku;
      return h('tr', {},
        h('td', {}, l.name),
        h('td', { class: 'num' }, money(l.unit_price)),
        h('td', {},
          h('label', { for: inputId, class: 'visually-hidden' }, 'Quantity for ' + l.name),
          h('input', {
            id: inputId, class: 'qty', type: 'number', min: '1', max: '999', value: String(l.qty),
            onchange: (ev) => {
              const qty = parseInt(ev.target.value, 10);
              const next = getCart();
              const line = next.find((x) => x.sku === l.sku);
              if (line && qty >= 1 && qty <= 999) { line.qty = qty; setCart(next); }
              viewCart();
            },
          })),
        h('td', { class: 'num' }, money(fromCents(centsOf(l.unit_price) * l.qty))),
        h('td', {}, h('button', {
          type: 'button', class: 'secondary', 'aria-label': 'Remove ' + l.name + ' from cart',
          onclick: () => { setCart(getCart().filter((x) => x.sku !== l.sku)); viewCart(); },
        }, 'Remove')));
    });
    const checkoutButton = h('button', { type: 'button' }, 'Proceed to checkout');
    checkoutButton.addEventListener('click', async () => {
      if (!getSession()) {
        sessionStorage.setItem('paylab.next', '#/cart');
        sessionStorage.setItem('paylab.flash', 'Please sign in to check out.');
        navigate('#/login');
        return;
      }
      checkoutButton.disabled = true;
      setBanner(msg, 'info', 'Creating your order…');
      const res = await api('POST', '/orders', { items: getCart().map((l) => ({ sku: l.sku, qty: l.qty })) });
      checkoutButton.disabled = false;
      if (res.status === 201) {
        setCart([]);
        navigate('#/checkout/' + encodeURIComponent(res.data.order_id));
      } else if (res.data && res.data.error === 'unknown_sku') {
        setBanner(msg, 'error', 'Some items in your cart are no longer available. Remove them and try again.');
      } else {
        setBanner(msg, 'error', errorMessage(res, 'Your order could not be created. Please try again.'));
      }
    });
    main.replaceChildren(
      h('h1', {}, 'Your cart'),
      msg,
      h('table', {},
        h('caption', { class: 'visually-hidden' }, 'Items in your cart'),
        h('thead', {}, h('tr', {},
          h('th', { scope: 'col' }, 'Product'), h('th', { scope: 'col', class: 'num' }, 'Unit price'),
          h('th', { scope: 'col' }, 'Quantity'), h('th', { scope: 'col', class: 'num' }, 'Line total'),
          h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
        h('tbody', {}, rows)),
      h('p', {}, 'Estimated subtotal: ', h('strong', {}, money(fromCents(subtotalCents)))),
      h('div', { class: 'actions' }, checkoutButton, h('a', { href: '#/products' }, 'Continue shopping')));
  }

  function itemsTable(order) {
    return h('table', {},
      h('caption', { class: 'visually-hidden' }, 'Items in order ' + order.order_id),
      h('thead', {}, h('tr', {},
        h('th', { scope: 'col' }, 'Product'), h('th', { scope: 'col', class: 'num' }, 'Qty'),
        h('th', { scope: 'col', class: 'num' }, 'Unit price'), h('th', { scope: 'col', class: 'num' }, 'Line total'))),
      h('tbody', {}, order.items.map((i) => h('tr', {},
        h('td', {}, i.name), h('td', { class: 'num' }, String(i.qty)),
        h('td', { class: 'num' }, money(i.unit_price)), h('td', { class: 'num' }, money(i.line_total))))));
  }

  function totalsList(order, displayTotal) {
    return h('dl', { class: 'totals' },
      h('dt', {}, 'Subtotal'), h('dd', { id: 'order-subtotal' }, money(order.subtotal)),
      h('dt', {}, order.voucher_code ? 'Discount (' + order.voucher_code + ')' : 'Discount'),
      h('dd', { id: 'order-discount' }, order.voucher_code ? '−' + money(order.discount) : money(order.discount)),
      h('dt', { class: 'grand' }, 'Total'),
      h('dd', { class: 'grand', 'data-testid': 'order-total' }, money(displayTotal)));
  }

  const VOUCHER_ERRORS = {
    voucher_not_found: 'We could not find that voucher code. Check the code and try again.',
    voucher_expired: 'This voucher has expired.',
    voucher_not_yet_valid: 'This voucher is not active yet.',
    voucher_exhausted: 'This voucher has already been fully redeemed.',
    voucher_already_applied: 'A voucher has already been applied to this order. Only one voucher can be used per order.',
    order_not_pending: 'Vouchers can only be added before payment.',
    validation_error: 'Enter a voucher code.',
  };

  const PAY_FAILURES = {
    declined: 'Your card was declined. Please use a different payment method.',
    provider_error: 'The payment provider had a problem processing your payment. You have not been charged. Please try again.',
    provider_timeout: 'The payment provider did not respond in time. You have not been charged. Please try again.',
  };

  const PAY_CONFLICTS = {
    payment_in_progress: 'A payment for this order is already in progress.',
    order_not_payable: 'This order can no longer be paid.',
    request_in_progress: 'Your payment is still being processed.',
    idempotency_key_reused: 'This payment request could not be processed. Please reload the page and try again.',
  };

  function payResultBanner(data) {
    if (data.status === 'processing') {
      return { kind: 'info', text: 'Payment processing… please wait.' };
    }
{% if bug("B12") %}
    if (data.status === 'payment_failed' && data.retryable) {
      return { kind: 'error', text: PAY_FAILURES[data.reason] || 'Payment failed. Please try again.' };
    }
    return { kind: 'success', text: 'Payment successful. Thank you for your order!' };
{% else %}
    if (data.status === 'payment_failed') {
      return { kind: 'error', text: PAY_FAILURES[data.reason] || 'Payment failed. Please try again.' };
    }
    return { kind: 'success', text: 'Payment successful. Thank you for your order!' };
{% endif %}
  }

  function finalStatusBanner(order) {
    switch (order.status) {
      case 'paid':
        return { kind: 'success', text: 'Payment successful. Thank you for your order!' };
      case 'payment_failed':
        return { kind: 'error', text: 'Your payment did not go through. Please try again or use a different payment method.' };
      case 'partially_refunded':
        return { kind: 'info', text: 'This order has been partially refunded.' };
      case 'refunded':
        return { kind: 'info', text: 'This order has been refunded.' };
      default:
        return { kind: 'info', text: 'Payment processing… please wait.' };
    }
  }

  async function viewCheckout(orderId) {
    setTitle('Checkout');
    const myView = viewToken;
    main.replaceChildren(h('h1', {}, 'Checkout'), h('p', {}, 'Loading your order…'));
    const res = await api('GET', '/orders/' + encodeURIComponent(orderId));
    if (myView !== viewToken) return;
    if (!res.ok) {
      const err = banner();
      main.replaceChildren(h('h1', {}, 'Checkout'), err);
      setBanner(err, 'error', errorMessage(res, 'Your order could not be loaded.'));
      return;
    }
    let order = res.data;
    let lastFailureRetryable = false;

    const totalsBox = h('div', { id: 'totals' });
    const voucherMsg = banner({ id: 'voucher-message' });
    const statusBanner = banner({ 'data-testid': 'status-banner', id: 'payment-status' });
    const voucherInput = h('input', { id: 'voucher-code', name: 'code', type: 'text', autocomplete: 'off' });
    const voucherButton = h('button', { type: 'submit' }, 'Apply voucher');
    const payButton = h('button', { type: 'button', id: 'pay-button' }, 'Pay now');
    const orderLink = h('a', { href: '#/orders/' + encodeURIComponent(order.order_id) }, 'View order details');

    function renderTotals() {
      const displayTotal = {% if bug("B07") %}order.voucher_code ? order.subtotal : order.total{% else %}order.total{% endif %};
      totalsBox.replaceChildren(totalsList(order, displayTotal));
    }

    function refreshControls() {
      const canVoucher = order.status === 'pending' && !order.voucher_code;
      voucherInput.disabled = !canVoucher;
      voucherButton.disabled = !canVoucher;
      const canPay = order.status === 'pending' || (order.status === 'payment_failed' && lastFailureRetryable);
      payButton.disabled = !canPay;
      payButton.textContent = order.status === 'payment_failed' ? 'Try payment again' : 'Pay now';
    }

    function show(result) { setBanner(statusBanner, result.kind, result.text); }

    function poll(startedAt) {
      pollTimer = setTimeout(async () => {
        if (myView !== viewToken) return;
        const r = await api('GET', '/orders/' + encodeURIComponent(order.order_id));
        if (myView !== viewToken) return;
        if (r.ok) {
          order = r.data;
          renderTotals();
          if (FINAL_STATUSES.includes(order.status)) {
            pollTimer = null;
            lastFailureRetryable = false;
            show(finalStatusBanner(order));
            refreshControls();
            return;
          }
        }
        if (Date.now() - startedAt > POLL_GIVE_UP_MS) {
          pollTimer = null;
          show({ kind: 'info', text: 'Your payment is still being processed. Check your order history for updates.' });
          return;
        }
        poll(startedAt);
      }, POLL_INTERVAL_MS);
    }

    const voucherForm = h('form', { class: 'inline-form', 'aria-label': 'Voucher' },
      h('div', { class: 'field' }, h('label', { for: 'voucher-code' }, 'Voucher code'), voucherInput),
      voucherButton);
    voucherForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const code = voucherInput.value.trim();
      if (!code) { setBanner(voucherMsg, 'error', VOUCHER_ERRORS.validation_error); return; }
      voucherButton.disabled = true;
      const r = await api('POST', '/orders/' + encodeURIComponent(order.order_id) + '/voucher', { code });
      if (myView !== viewToken) return;
      if (r.ok) {
        order = r.data;
        renderTotals();
        setBanner(voucherMsg, 'success', 'Voucher ' + order.voucher_code + ' applied. You save ' + money(order.discount) + '.');
      } else {
        const errorCode = r.data && r.data.error;
        setBanner(voucherMsg, 'error', VOUCHER_ERRORS[errorCode] || errorMessage(r, 'The voucher could not be applied. Please try again.'));
      }
      refreshControls();
    });

    payButton.addEventListener('click', async () => {
      stopPolling();
      payButton.disabled = true;
      voucherButton.disabled = true;
      voucherInput.disabled = true;
      show({ kind: 'info', text: 'Submitting your payment…' });
      const key = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
      const r = await api('POST', '/orders/' + encodeURIComponent(order.order_id) + '/pay', undefined, { 'Idempotency-Key': key });
      if (myView !== viewToken) return;
      if (r.status === 202 || (r.status === 200 && r.data)) {
        show(payResultBanner(r.data));
        if (r.data.status === 'processing') {
          order.status = 'processing';
          refreshControls();
          poll(Date.now());
          return;
        }
        order.status = r.data.status;
        lastFailureRetryable = Boolean(r.data.retryable);
      } else if (r.status === 409 && r.data) {
        show({ kind: 'error', text: PAY_CONFLICTS[r.data.error] || 'This order cannot be paid right now.' });
      } else {
        show({ kind: 'error', text: errorMessage(r, 'Payment could not be submitted. Please try again.') });
        lastFailureRetryable = true;
      }
      refreshControls();
    });

    renderTotals();
    refreshControls();
    if (order.status === 'processing') {
      show(finalStatusBanner(order));
      poll(Date.now());
    } else if (order.status !== 'pending') {
      show(finalStatusBanner(order));
    }

    main.replaceChildren(
      h('h1', {}, 'Checkout'),
      h('p', { class: 'muted' }, 'Order ', h('strong', {}, order.order_id)),
      h('section', { class: 'card', 'aria-labelledby': 'summary-heading' },
        h('h2', { id: 'summary-heading' }, 'Order summary'), itemsTable(order)),
      h('section', { class: 'card', 'aria-labelledby': 'voucher-heading' },
        h('h2', { id: 'voucher-heading' }, 'Voucher'), voucherForm, voucherMsg),
      h('section', { class: 'card', 'aria-labelledby': 'total-heading' },
        h('h2', { id: 'total-heading' }, 'Total'), totalsBox),
      h('section', { class: 'card', 'aria-labelledby': 'payment-heading' },
        h('h2', { id: 'payment-heading' }, 'Payment'),
        h('div', { class: 'actions' }, payButton, orderLink),
        statusBanner));
  }

  async function viewOrders(params) {
    setTitle('Order history');
    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
    const msg = banner();
    main.replaceChildren(h('h1', {}, 'Order history'), h('p', {}, 'Loading orders…'));
    const res = await api('GET', '/orders?page=' + page + '&limit=' + PAGE_SIZE);
    if (!res.ok) {
      main.replaceChildren(h('h1', {}, 'Order history'), msg);
      setBanner(msg, 'error', errorMessage(res, 'Your orders could not be loaded.'));
      return;
    }
    const data = res.data;
    if (data.total_items === 0) {
      main.replaceChildren(h('h1', {}, 'Order history'),
        h('p', {}, 'You have not placed any orders yet. ', h('a', { href: '#/products' }, 'Start shopping')));
      return;
    }
    const isAdmin = getSession() && getSession().role === 'admin';
    const pages = Math.max(1, Math.ceil(data.total_items / data.limit));
    main.replaceChildren(
      h('h1', {}, 'Order history'),
      h('table', {},
        h('caption', { class: 'visually-hidden' }, 'Your orders, page ' + page + ' of ' + pages),
        h('thead', {}, h('tr', {},
          h('th', { scope: 'col' }, 'Order'), h('th', { scope: 'col' }, 'Placed'),
          isAdmin ? h('th', { scope: 'col' }, 'Customer') : null,
          h('th', { scope: 'col' }, 'Status'), h('th', { scope: 'col', class: 'num' }, 'Total'))),
        h('tbody', {}, data.items.map((o) => h('tr', {},
          h('td', {}, h('a', { href: '#/orders/' + encodeURIComponent(o.order_id) }, o.order_id)),
          h('td', {}, formatDate(o.created_at)),
          isAdmin ? h('td', {}, o.customer) : null,
          h('td', {}, h('span', { class: 'status-pill' }, statusLabel(o.status))),
          h('td', { class: 'num' }, money(o.total)))))),
      pagination('#/orders', page, pages));
  }

  function paymentsTable(payments) {
    if (!payments.length) return h('p', { class: 'muted' }, 'No payments recorded.');
    return h('table', {},
      h('caption', { class: 'visually-hidden' }, 'Payments'),
      h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Payment'), h('th', { scope: 'col' }, 'Date'),
        h('th', { scope: 'col' }, 'Status'), h('th', { scope: 'col', class: 'num' }, 'Amount'))),
      h('tbody', {}, payments.map((p) => h('tr', {}, h('td', {}, p.payment_id), h('td', {}, formatDate(p.created_at)),
        h('td', {}, p.status), h('td', { class: 'num' }, money(p.amount))))));
  }

  function refundsTable(refunds) {
    if (!refunds.length) return h('p', { class: 'muted' }, 'No refunds issued.');
    return h('table', {},
      h('caption', { class: 'visually-hidden' }, 'Refunds'),
      h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Refund'), h('th', { scope: 'col' }, 'Date'),
        h('th', { scope: 'col' }, 'Issued by'), h('th', { scope: 'col', class: 'num' }, 'Amount'))),
      h('tbody', {}, refunds.map((r) => h('tr', {}, h('td', {}, r.refund_id), h('td', {}, formatDate(r.created_at)),
        h('td', {}, r.created_by), h('td', { class: 'num' }, money(r.amount))))));
  }

  async function loadOrderBundle(orderId) {
    const id = encodeURIComponent(orderId);
    const [o, p, r] = await Promise.all([
      api('GET', '/orders/' + id), api('GET', '/orders/' + id + '/payments'), api('GET', '/orders/' + id + '/refunds'),
    ]);
    return { order: o, payments: p, refunds: r };
  }

  async function viewOrderDetail(orderId) {
    setTitle('Order details');
    const myView = viewToken;
    main.replaceChildren(h('h1', {}, 'Order details'), h('p', {}, 'Loading order…'));
    const bundle = await loadOrderBundle(orderId);
    if (myView !== viewToken) return;
    if (!bundle.order.ok) {
      const err = banner();
      main.replaceChildren(h('h1', {}, 'Order details'), err);
      setBanner(err, 'error', errorMessage(bundle.order, 'This order could not be loaded.'));
      return;
    }
    const order = bundle.order.data;
    const canContinue = order.status === 'pending' || order.status === 'payment_failed';
    main.replaceChildren(
      h('h1', {}, 'Order ' + order.order_id),
      h('p', {}, 'Status: ', h('span', { class: 'status-pill' }, statusLabel(order.status)),
        ' · Placed ' + formatDate(order.created_at) + ' by ' + order.customer),
      canContinue ? h('p', {}, h('a', { class: 'button', href: '#/checkout/' + encodeURIComponent(order.order_id) }, 'Continue to checkout')) : null,
      h('section', { class: 'card', 'aria-labelledby': 'items-heading' },
        h('h2', { id: 'items-heading' }, 'Items'), itemsTable(order), totalsList(order, order.total)),
      h('section', { class: 'card', 'aria-labelledby': 'payments-heading' },
        h('h2', { id: 'payments-heading' }, 'Payments'), paymentsTable(bundle.payments.ok ? bundle.payments.data : [])),
      h('section', { class: 'card', 'aria-labelledby': 'refunds-heading' },
        h('h2', { id: 'refunds-heading' }, 'Refunds'), refundsTable(bundle.refunds.ok ? bundle.refunds.data : [])));
  }

  const REFUND_ERRORS = {
    refund_exceeds_payment: 'That refund is more than the amount still available to refund on this order.',
    order_not_refundable: 'Only paid orders can be refunded.',
    validation_error: 'Enter a refund amount greater than zero, with at most two decimal places.',
  };

  async function viewAdminRefunds(orderId) {
    setTitle('Refunds');
    const session = getSession();
    if (!session || session.role !== 'admin') {
      const err = banner();
      main.replaceChildren(h('h1', {}, 'Refunds'), err);
      setBanner(err, 'error', 'You need an administrator account to issue refunds.');
      return;
    }
    const myView = viewToken;
    const lookupInput = h('input', { id: 'refund-order-id', type: 'text', value: orderId || '', autocomplete: 'off' });
    const lookupForm = h('form', { class: 'inline-form', 'aria-label': 'Find order' },
      h('div', { class: 'field' }, h('label', { for: 'refund-order-id' }, 'Order ID'), lookupInput),
      h('button', { type: 'submit' }, 'Find order'));
    lookupForm.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const id = lookupInput.value.trim();
      if (id) navigate('#/admin/refunds/' + encodeURIComponent(id));
    });
    const msg = banner({ id: 'refund-message' });
    const detail = h('div', {});
    main.replaceChildren(h('h1', {}, 'Refunds'), h('section', { class: 'card' }, lookupForm), msg, detail);
    if (!orderId) return;

    const bundle = await loadOrderBundle(orderId);
    if (myView !== viewToken) return;
    if (!bundle.order.ok) {
      setBanner(msg, 'error', errorMessage(bundle.order, 'This order could not be loaded.'));
      return;
    }
    const order = bundle.order.data;
    const payments = bundle.payments.ok ? bundle.payments.data : [];
    const refunds = bundle.refunds.ok ? bundle.refunds.data : [];
    const paidCents = payments.reduce((s, p) => s + centsOf(p.amount), 0);
    const refundedCents = refunds.reduce((s, r) => s + centsOf(r.amount), 0);
    const amountInput = h('input', { id: 'refund-amount', type: 'text', inputmode: 'decimal', autocomplete: 'off' });
    const refundButton = h('button', { type: 'submit' }, 'Issue refund');
    const refundForm = h('form', { class: 'inline-form', 'aria-label': 'Issue refund' },
      h('div', { class: 'field' }, h('label', { for: 'refund-amount' }, 'Refund amount (CAD)'), amountInput),
      refundButton);
    refundForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const amount = amountInput.value.trim();
      if (!/^\d+(\.\d{1,2})?$/.test(amount) || centsOf(amount) <= 0) {
        setBanner(msg, 'error', REFUND_ERRORS.validation_error);
        return;
      }
      refundButton.disabled = true;
      const r = await api('POST', '/orders/' + encodeURIComponent(order.order_id) + '/refunds', { amount });
      refundButton.disabled = false;
      if (myView !== viewToken) return;
      if (r.status === 201) {
        await viewAdminRefunds(order.order_id);
        setBanner(document.getElementById('refund-message'), 'success',
          'Refund of ' + money(r.data.amount) + ' issued. Order is now ' + statusLabel(r.data.order_status).toLowerCase() + '.');
      } else {
        const code = r.data && r.data.error;
        setBanner(msg, 'error', REFUND_ERRORS[code] || errorMessage(r, 'The refund could not be issued.'));
      }
    });
    detail.replaceChildren(
      h('section', { class: 'card', 'aria-labelledby': 'refund-order-heading' },
        h('h2', { id: 'refund-order-heading' }, 'Order ' + order.order_id),
        h('p', {}, 'Customer: ' + order.customer + ' · Status: ', h('span', { class: 'status-pill' }, statusLabel(order.status))),
        h('dl', { class: 'totals' },
          h('dt', {}, 'Paid'), h('dd', { id: 'refund-paid' }, money(fromCents(paidCents))),
          h('dt', {}, 'Refunded so far'), h('dd', { id: 'refund-refunded' }, money(fromCents(refundedCents))),
          h('dt', {}, 'Available to refund'), h('dd', { id: 'refund-available' }, money(fromCents(Math.max(0, paidCents - refundedCents)))))),
      h('section', { class: 'card', 'aria-labelledby': 'refund-form-heading' },
        h('h2', { id: 'refund-form-heading' }, 'Issue a refund'), refundForm),
      h('section', { class: 'card', 'aria-labelledby': 'refund-history-heading' },
        h('h2', { id: 'refund-history-heading' }, 'Refund history'), refundsTable(refunds)));
  }

  function viewNotFound() {
    setTitle('Page not found');
    main.replaceChildren(h('h1', {}, 'Page not found'), h('p', {}, h('a', { href: '#/products' }, 'Go to products')));
  }

  // ---------- router ----------
  const PROTECTED = ['checkout', 'orders', 'admin'];

  function render() {
    stopPolling();
    viewToken += 1;
    const raw = location.hash.replace(/^#/, '') || '/products';
    const [pathPart, query] = raw.split('?');
    const params = new URLSearchParams(query || '');
    const parts = pathPart.split('/').filter(Boolean).map(decodeURIComponent);
    const section = parts[0] || 'products';

    if (PROTECTED.includes(section) && !getSession()) {
      sessionStorage.setItem('paylab.next', '#' + raw);
      sessionStorage.setItem('paylab.flash', 'Please sign in to continue.');
      location.hash = '#/login';
      return;
    }

    if (section === 'login') viewLogin();
    else if (section === 'products') viewProducts(params);
    else if (section === 'cart') viewCart();
    else if (section === 'checkout' && parts[1]) viewCheckout(parts[1]);
    else if (section === 'orders' && parts[1]) viewOrderDetail(parts[1]);
    else if (section === 'orders') viewOrders(params);
    else if (section === 'admin' && parts[1] === 'refunds') viewAdminRefunds(parts[2]);
    else viewNotFound();
  }

  document.getElementById('logout-button').addEventListener('click', () => {
    setSession(null);
    navigate('#/login');
  });
  window.addEventListener('hashchange', () => { render(); main.focus(); });
  window.addEventListener('storage', renderHeader);
  renderHeader();
  render();
})();
