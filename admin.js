const params = new URLSearchParams(window.location.search);
const adminKey = params.get('admin_key') || '';

const els = {
  products: document.getElementById('products'),
  reservations: document.getElementById('reservations'),
  messages: document.getElementById('messages'),
  toast: document.getElementById('toast'),
  refreshBtn: document.getElementById('refreshBtn'),
  setupBtn: document.getElementById('setupBtn'),
  expireBtn: document.getElementById('expireBtn'),
  resetBtn: document.getElementById('resetBtn'),
  operatorUsername: document.getElementById('operatorUsername'),
  instructionText: document.getElementById('instructionText'),
  saveSettingsBtn: document.getElementById('saveSettingsBtn'),
};

async function api(path, options = {}) {
  const separator = path.includes('?') ? '&' : '?';
  const response = await fetch(`${path}${separator}admin_key=${encodeURIComponent(adminKey)}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Ошибка запроса');
  return data;
}

async function uploadApi(path, formData) {
  const separator = path.includes('?') ? '&' : '?';
  const response = await fetch(`${path}${separator}admin_key=${encodeURIComponent(adminKey)}`, {
    method: 'POST',
    body: formData,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Ошибка загрузки');
  return data;
}

function showToast(text) {
  els.toast.textContent = text;
  els.toast.classList.remove('hidden');
  setTimeout(() => els.toast.classList.add('hidden'), 2600);
}

function escapeHtml(value) {
  return String(value || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function fmt(value) {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleString('ru-RU');
  } catch {
    return value;
  }
}

async function loadSettings() {
  const data = await api('/api/admin/settings');
  els.operatorUsername.value = data.operator_username || '';
  els.instructionText.value = data.instruction || '';
}

async function saveSettings() {
  try {
    await api('/api/admin/settings', {
      method: 'PUT',
      body: JSON.stringify({
        operator_username: els.operatorUsername.value.trim(),
        instruction: els.instructionText.value.trim(),
      }),
    });
    showToast('Общая инструкция сохранена');
  } catch (error) {
    showToast(error.message);
  }
}

async function loadProducts() {
  const data = await api('/api/admin/products');
  els.products.innerHTML = data.items.map(product => {
    const image = product.image_url
      ? `<img src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.title)}" />`
      : '<span class="placeholder">📦</span>';
    const stockText = product.available > 0
      ? `Осталось ${product.available} из ${product.daily_limit}`
      : `Мест нет из ${product.daily_limit}`;
    return `
      <article class="admin-product" data-id="${product.id}">
        <div class="admin-product-top">
          <div class="admin-product-preview">
            <span class="stock-overlay ${product.available <= 0 ? 'empty' : ''}">${stockText}</span>
            ${image}
          </div>
          <div class="admin-product-heading">
            <strong>${escapeHtml(product.title)} <span class="muted">${escapeHtml(product.sku)}</span></strong>
            <small class="muted">Сегодня: выполнено ${product.completed_count}, бронь ${product.reserved_count}, доступно ${product.available}</small>
          </div>
        </div>

        <label class="upload-label">
          <span>Загрузить фото с компьютера/телефона</span>
          <input name="image_file" type="file" accept="image/png,image/jpeg,image/webp" onchange="uploadProductImage(${product.id}, this)" />
        </label>

        <input name="sku" value="${escapeHtml(product.sku)}" placeholder="Артикул/SKU" />
        <input name="title" value="${escapeHtml(product.title)}" placeholder="Название" />
        <textarea name="description" placeholder="Короткое описание товара на карточке">${escapeHtml(product.description)}</textarea>
        <textarea name="instruction" class="instruction-editor product-instruction" placeholder="Инструкция именно для этого артикула. Ее человек увидит после брони этого товара.">${escapeHtml(product.instruction || '')}</textarea>
        <input name="image_url" value="${escapeHtml(product.image_url)}" placeholder="Ссылка на фото или путь после загрузки" />
        <input name="marketplace_url" value="${escapeHtml(product.marketplace_url)}" placeholder="Ссылка на карточку товара" />
        <input name="daily_limit" type="number" min="0" max="10000" value="${product.daily_limit}" placeholder="Лимит на день" />
        <label><input name="is_active" type="checkbox" ${product.is_active ? 'checked' : ''} /> Активен</label>
        <button class="primary-btn" type="button" onclick="saveProduct(${product.id})">Сохранить</button>
      </article>
    `;
  }).join('');
}

async function uploadProductImage(id, input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    const formData = new FormData();
    formData.append('file', file);
    await uploadApi(`/api/admin/products/${id}/image`, formData);
    showToast('Фото загружено');
    await loadProducts();
  } catch (error) {
    showToast(error.message);
  } finally {
    input.value = '';
  }
}

async function saveProduct(id) {
  const card = document.querySelector(`.admin-product[data-id="${id}"]`);
  const body = {
    sku: card.querySelector('[name="sku"]').value.trim(),
    title: card.querySelector('[name="title"]').value.trim(),
    description: card.querySelector('[name="description"]').value.trim(),
    instruction: card.querySelector('[name="instruction"]').value.trim(),
    image_url: card.querySelector('[name="image_url"]').value.trim(),
    marketplace_url: card.querySelector('[name="marketplace_url"]').value.trim(),
    daily_limit: Number(card.querySelector('[name="daily_limit"]').value || 0),
    is_active: card.querySelector('[name="is_active"]').checked,
  };
  try {
    await api(`/api/admin/products/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    showToast('Артикул сохранен');
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
}

async function loadReservations() {
  const data = await api('/api/admin/reservations');
  els.reservations.innerHTML = data.items.length ? data.items.map(item => {
    const username = item.username ? `@${item.username}` : '';
    const name = `${item.first_name || ''} ${item.last_name || ''}`.trim();
    return `
      <tr>
        <td>#${item.id}</td>
        <td><strong>${escapeHtml(item.sku)}</strong><br>${escapeHtml(item.title)}</td>
        <td>${escapeHtml(name || '-')}${username ? `<br><span class="muted">${escapeHtml(username)}</span>` : ''}<br><span class="muted">ID ${item.telegram_id}</span></td>
        <td><span class="tag ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td>
        <td>${fmt(item.reserved_at)}</td>
        <td>${fmt(item.expires_at)}</td>
        <td>${fmt(item.completed_at)}</td>
      </tr>
    `;
  }).join('') : '<tr><td colspan="7" class="muted">Заявок за сегодня пока нет.</td></tr>';
}

async function loadMessages() {
  const data = await api('/api/admin/messages');
  els.messages.innerHTML = data.items.length ? data.items.map(item => {
    const username = item.username ? `@${item.username}` : '';
    const name = `${item.first_name || ''} ${item.last_name || ''}`.trim();
    return `
      <tr>
        <td>${fmt(item.created_at)}</td>
        <td>${escapeHtml(name || '-')}${username ? `<br><span class="muted">${escapeHtml(username)}</span>` : ''}<br><span class="muted">ID ${item.telegram_id}</span></td>
        <td>${escapeHtml(item.text)}</td>
      </tr>
    `;
  }).join('') : '<tr><td colspan="3" class="muted">Сообщений боту пока нет.</td></tr>';
}

async function loadAll() {
  try {
    await Promise.all([loadSettings(), loadProducts(), loadReservations(), loadMessages()]);
  } catch (error) {
    showToast(error.message);
  }
}

els.refreshBtn.addEventListener('click', loadAll);
els.saveSettingsBtn.addEventListener('click', saveSettings);
els.setupBtn.addEventListener('click', async () => {
  try {
    await api('/api/admin/setup-telegram', { method: 'POST' });
    showToast('Telegram подключен: webhook и меню установлены');
  } catch (error) {
    showToast(error.message);
  }
});
els.expireBtn.addEventListener('click', async () => {
  if (!confirm('Все активные брони станут просроченными. Продолжить?')) return;
  await api('/api/admin/expire-now', { method: 'POST' });
  showToast('Активные брони освобождены');
  await loadAll();
});
els.resetBtn.addEventListener('click', async () => {
  if (!confirm('Удалить все заявки за сегодня? Это действие нельзя отменить.')) return;
  await api('/api/admin/reset-day', { method: 'POST' });
  showToast('Заявки за сегодня очищены');
  await loadAll();
});

setInterval(() => {
  loadReservations();
  loadMessages();
}, 20000);
loadAll();
