const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const state = {
  products: [],
  reservation: null,
  instruction: '',
  operatorUsername: '',
  completed: null,
  timerId: null,
};

const els = {
  products: document.getElementById('products'),
  activeReservation: document.getElementById('activeReservation'),
  completedMessage: document.getElementById('completedMessage'),
  instruction: document.getElementById('instruction'),
  toast: document.getElementById('toast'),
  refreshBtn: document.getElementById('refreshBtn'),
};

function initData() {
  return tg?.initData || '';
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': initData(),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || 'Ошибка запроса');
  }
  return data;
}

function showToast(text) {
  els.toast.textContent = text;
  els.toast.classList.remove('hidden');
  setTimeout(() => els.toast.classList.add('hidden'), 2800);
}

function formatTimeLeft(expiresAt) {
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return '00:00';
  const total = Math.floor(ms / 1000);
  const min = String(Math.floor(total / 60)).padStart(2, '0');
  const sec = String(total % 60).padStart(2, '0');
  return `${min}:${sec}`;
}

function escapeHtml(value) {
  return String(value || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function renderProducts() {
  if (!state.products.length) {
    els.products.innerHTML = '<div class="empty-state">Пока нет активных артикулов.</div>';
    return;
  }
  els.products.innerHTML = state.products.map(product => {
    const used = product.daily_limit - product.available;
    const percent = product.daily_limit ? Math.min(100, Math.round((used / product.daily_limit) * 100)) : 0;
    const disabled = product.available <= 0 || !!state.reservation;
    const image = product.image_url
      ? `<img src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.title)}" />`
      : '<span class="placeholder">📦</span>';
    const stockText = product.available > 0
      ? `Осталось ${product.available} из ${product.daily_limit}`
      : `Мест нет из ${product.daily_limit}`;
    return `
      <article class="product-card">
        <div class="product-image">
          <span class="stock-overlay ${product.available <= 0 ? 'empty' : ''}">${stockText}</span>
          ${image}
        </div>
        <div class="product-content">
          <div class="product-header">
            <div>
              <h2 class="product-title">${escapeHtml(product.title)}</h2>
              <div class="sku">${escapeHtml(product.sku)}</div>
            </div>
          </div>
          <p class="description">${escapeHtml(product.description || 'Описание будет здесь.')}</p>
          <div class="progress-wrap">
            <div class="progress-line"><span>Занято ${used} из ${product.daily_limit}</span><span>${percent}%</span></div>
            <div class="progress"><span style="width:${percent}%"></span></div>
          </div>
          <div class="actions">
            <button class="primary-btn" type="button" ${disabled ? 'disabled' : ''} onclick="reserve(${product.id})">Забронировать</button>
          </div>
        </div>
      </article>
    `;
  }).join('');
}

function renderInstruction() {
  if (!state.reservation) {
    els.instruction.classList.add('hidden');
    els.instruction.innerHTML = '';
    return;
  }
  const lines = state.instruction.split('\n').filter(Boolean).map(line => line.replace(/^\d+\.\s*/, ''));
  els.instruction.classList.remove('hidden');
  const title = state.reservation?.title ? ` по товару «${escapeHtml(state.reservation.title)}»` : '';
  els.instruction.innerHTML = `
    <h2>Инструкция${title}</h2>
    <ol>${lines.map(line => `<li>${escapeHtml(line)}</li>`).join('')}</ol>
  `;
}

function renderCompleted() {
  if (!state.completed) {
    els.completedMessage.classList.add('hidden');
    els.completedMessage.innerHTML = '';
    return;
  }
  const operator = state.completed.operator_username || state.operatorUsername || '';
  const operatorLine = operator ? `оператору @${escapeHtml(operator)}` : 'оператору, который прислал ссылку';
  const operatorLink = operator ? `<a class="button-link secondary-btn" href="https://t.me/${escapeHtml(operator)}" target="_blank" rel="noopener">Написать @${escapeHtml(operator)}</a>` : '';
  els.completedMessage.classList.remove('hidden');
  els.completedMessage.innerHTML = `
    <article class="completed-card">
      <div class="success-icon">✓</div>
      <h2>${escapeHtml(state.completed.title || 'Готово')}</h2>
      <p class="muted">${escapeHtml(state.completed.text || 'Выполнение зафиксировано.')}</p>
      <div class="copy-box">
        <span class="status-label">Сообщение для отправки ${operatorLine}</span>
        <p>${escapeHtml(state.completed.copy_message || 'Здравствуйте, товар выкуплен.')}</p>
      </div>
      <div class="actions">
        <button class="primary-btn" type="button" onclick="copyFinalMessage()">Скопировать сообщение</button>
        ${operatorLink}
      </div>
    </article>
  `;
}

async function copyFinalMessage() {
  const text = state.completed?.copy_message || 'Здравствуйте, товар выкуплен.';
  try {
    await navigator.clipboard.writeText(text);
    showToast('Сообщение скопировано');
  } catch {
    showToast('Скопируйте сообщение вручную');
  }
}

function renderReservation() {
  if (!state.reservation) {
    els.activeReservation.classList.add('hidden');
    els.activeReservation.innerHTML = '';
    if (state.timerId) clearInterval(state.timerId);
    renderInstruction();
    return;
  }

  const r = state.reservation;
  els.activeReservation.classList.remove('hidden');
  els.activeReservation.innerHTML = `
    <article class="reservation-card">
      <div class="reservation-top">
        <div>
          <h2>Ваша бронь активна</h2>
          <p class="muted">${escapeHtml(r.title)} · ${escapeHtml(r.sku)}</p>
        </div>
        <div class="timer" id="timer">${formatTimeLeft(r.expires_at)}</div>
      </div>
      <div class="status-grid">
        <div class="status-item"><span class="status-label">Статус</span><span class="status-value">Бронь</span></div>
        <div class="status-item"><span class="status-label">Номер заявки</span><span class="status-value">#${r.id}</span></div>
      </div>
      <div class="actions" style="margin-top:12px">
        ${r.marketplace_url ? `<a class="button-link secondary-btn" href="${escapeHtml(r.marketplace_url)}" target="_blank" rel="noopener">Открыть карточку</a>` : ''}
        <button class="primary-btn" type="button" onclick="completeReservation(${r.id})">Товар выкуплен</button>
        <button class="danger-btn" type="button" onclick="cancelReservation(${r.id})">Отменить</button>
      </div>
    </article>
  `;

  renderInstruction();

  if (state.timerId) clearInterval(state.timerId);
  state.timerId = setInterval(async () => {
    const timer = document.getElementById('timer');
    if (!timer) return;
    const left = formatTimeLeft(r.expires_at);
    timer.textContent = left;
    if (left === '00:00') {
      clearInterval(state.timerId);
      showToast('Время брони истекло. Место освобождено.');
      state.reservation = null;
      await loadAll();
    }
  }, 1000);
}

async function loadAll() {
  els.products.innerHTML = '<div class="loading">Загружаю доступные артикулы...</div>';
  try {
    const productsData = await api('/api/products');
    state.products = productsData.items;
    try {
      const my = await api('/api/my-reservation');
      state.reservation = my.reservation;
      state.instruction = my.instruction || '';
      state.operatorUsername = my.operator_username || '';
    } catch (error) {
      state.reservation = null;
      state.instruction = '';
    }
    renderCompleted();
    renderReservation();
    renderProducts();
  } catch (error) {
    els.products.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

async function reserve(productId) {
  try {
    state.completed = null;
    const data = await api('/api/reservations', {
      method: 'POST',
      body: JSON.stringify({ product_id: productId }),
    });
    state.reservation = data.reservation;
    state.instruction = data.instruction || '';
    state.operatorUsername = data.operator_username || '';
    showToast('Бронь создана на 50 минут');
    await loadAll();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (error) {
    showToast(error.message);
    await loadAll();
  }
}

async function completeReservation(reservationId) {
  try {
    const data = await api('/api/reservations/complete', {
      method: 'POST',
      body: JSON.stringify({ reservation_id: reservationId }),
    });
    state.reservation = null;
    state.completed = data.confirmation;
    showToast('Готово, выполнение зафиксировано');
    await loadAll();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (error) {
    showToast(error.message);
    await loadAll();
  }
}

async function cancelReservation(reservationId) {
  const ok = tg?.showConfirm
    ? await new Promise(resolve => tg.showConfirm('Отменить бронь?', resolve))
    : confirm('Отменить бронь?');
  if (!ok) return;
  try {
    await api('/api/reservations/cancel', {
      method: 'POST',
      body: JSON.stringify({ reservation_id: reservationId }),
    });
    state.reservation = null;
    state.completed = null;
    showToast('Бронь отменена');
    await loadAll();
  } catch (error) {
    showToast(error.message);
    await loadAll();
  }
}

els.refreshBtn.addEventListener('click', loadAll);
setInterval(loadAll, 30000);
loadAll();
