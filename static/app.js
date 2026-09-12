/* SPA Logic for Arcie Bot Web3 Giveaway Hub */

// Firebase Database URL (HTTPS — works from Vercel)
const FIREBASE_DB = 'https://arcie-bot-default-rtdb.asia-southeast1.firebasedatabase.app';
const ADMIN_PASSWORD = 'innercirclefcfs78@1';

// Helper: API URL resolver (prepends window.ARCIE_API_BASE if hosted remotely/Vercel)
function apiUrl(path) {
  if (!path) return '';
  if (path.startsWith('http://') || path.startsWith('https://')) return path;
  const base = (typeof window !== 'undefined' && window.ARCIE_API_BASE) ? window.ARCIE_API_BASE.replace(/\/+$/, '') : '';
  const cleanPath = path.startsWith('/') ? path : '/' + path;
  return base ? `${base}${cleanPath}` : cleanPath;
}

let currentUser = null;
let currentGiveaways = [];
let currentFilter = 'active';
let activeDetailGiveaway = null;

// Helper: Firebase REST read with in-memory ETag caching (0 payload bytes on 304)
const _fbEtagCache = {};

async function firebaseGet(path) {
  const normPath = path.replace(/^\/+|\/+$/g, '');
  const cached = _fbEtagCache[normPath];
  const headers = { 'X-Firebase-ETag': 'true' };
  if (cached && cached.etag) {
    headers['If-None-Match'] = cached.etag;
  }
  try {
    const res = await fetch(`${FIREBASE_DB}/${normPath}.json`, { headers });
    if (res.status === 304 && cached) {
      return cached.data;
    }
    if (res.ok) {
      const data = await res.json();
      const etag = res.headers.get('ETag') || res.headers.get('etag');
      if (etag) {
        _fbEtagCache[normPath] = { etag, data };
      }
      return data;
    }
  } catch (err) {
    console.warn(`Firebase GET ${normPath} failed:`, err);
    if (cached) return cached.data;
  }
  return null;
}

// Helper: Firebase REST write
async function firebasePut(path, data) {
  const normPath = path.replace(/^\/+|\/+$/g, '');
  delete _fbEtagCache[normPath];
  // Invalidate any parent/child paths in cache
  Object.keys(_fbEtagCache).forEach(k => {
    if (k.startsWith(normPath + '/') || normPath.startsWith(k + '/')) {
      delete _fbEtagCache[k];
    }
  });

  // Clone data to avoid mutating the original, and strip excessively large base64 banner_url
  // strings (>500KB) from Firebase to prevent quota abuse, but preserve smaller images
  // so the bot can convert them to local files on sync.
  let cleanData = data;
  if (data && typeof data === 'object') {
    cleanData = JSON.parse(JSON.stringify(data));
    const sanitizeLargeBlobs = (obj) => {
      if (!obj || typeof obj !== 'object') return;
      for (const k in obj) {
        if (k === 'banner_url' && typeof obj[k] === 'string' && obj[k].startsWith('data:image') && obj[k].length > 500000) {
          // Only strip if >500KB (extremely large base64) — smaller ones are kept for bot processing
          obj[k] = '';
        } else if (typeof obj[k] === 'object') {
          sanitizeLargeBlobs(obj[k]);
        }
      }
    };
    sanitizeLargeBlobs(cleanData);
  }

  await fetch(`${FIREBASE_DB}/${normPath}.json`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cleanData)
  });
}

// Helper: Format Markdown (Bold, Italics, Code, Links) for Web Display
function formatMarkdownDescription(text) {
  if (!text) return '';
  let str = escapeHtml(text);

  // 1. Markdown Links: [label](url)
  str = str.replace(/\[([^\]]+)\]\((https?:\/\/[^\s\)]+)\)/g, (match, label, url) => {
    return `<a href="${url}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: underline; font-weight: 600;">${label}</a>`;
  });

  // 2. Raw URLs (not already inside href="...")
  str = str.replace(/(^|[^"])((https?:\/\/[^\s<]+))/g, (match, prefix, fullUrl) => {
    if (prefix.includes('href=') || prefix.includes('src=')) return match;
    return `${prefix}<a href="${fullUrl}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: underline; font-weight: 600;">Click Here</a>`;
  });

  // 3. Bold: **text**
  str = str.replace(/\*\*([^*]+)\*\*/g, '<b style="color: #fff; font-weight: 700;">$1</b>');

  // 4. Italics: *text*
  str = str.replace(/\*([^*]+)\*/g, '<i>$1</i>');

  // 5. Code: `code`
  str = str.replace(/`([^`]+)`/g, '<code style="background: rgba(0,0,0,0.4); padding: 2px 6px; border-radius: 4px; color: #a78bfa;">$1</code>');

  // 6. Newlines to <br>
  str = str.replace(/\n/g, '<br>');

  return str;
}

// Render Social Links HTML Buttons for Web Display (Tessera Clean Style)
function renderSocialButtonsHTML(social_links) {
  if (!social_links || typeof social_links !== 'object') return '';
  const btns = [];
  if (social_links.twitter_link && social_links.twitter_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.twitter_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 3px 8px; font-size: 0.72rem;">${svgTwitter()} X / Twitter</a>`);
  }
  if (social_links.discord_link && social_links.discord_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.discord_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 3px 8px; font-size: 0.72rem;">${svgDiscord()} Discord</a>`);
  }
  if (social_links.telegram_link && social_links.telegram_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.telegram_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 3px 8px; font-size: 0.72rem;">${svgTelegram()} Telegram</a>`);
  }
  if (social_links.website_link && social_links.website_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.website_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 3px 8px; font-size: 0.72rem;">${svgGlobe()} Website</a>`);
  }
  if (!btns.length) return '';
  return `<div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px;">${btns.join('')}</div>`;
}

document.addEventListener('DOMContentLoaded', () => {
  initApp();
  setupEventListeners();
});

async function initApp() {
  checkAuth();
  await loadGiveaways();
  await loadGuildChannels();
  await loadGuildRoles();
  await checkUrlDirectGiveaway();
}

function setupEventListeners() {
  // Tab filters
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      currentFilter = e.target.dataset.tab;
      renderGiveaways();
    });
  });

  // Create Giveaway Button
  const createBtn = document.getElementById('createGiveawayBtn');
  if (createBtn) {
    createBtn.addEventListener('click', () => {
      createRequiredRoles = [];
      renderCreateRequiredRoles();
      loadGuildChannels();
      openModal('createModal');
    });
  }
}

let cachedServerChannels = [];
let cachedServerRoles = [];

// Filter & populate channel select dropdowns based on search query
function filterChannelSelect(selectId, query) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const q = (query || '').trim().toLowerCase();

  let baseOpt = '';
  if (selectId === 'gChannel') {
    baseOpt = '<option value="auto">Auto-Detect Main Channel</option>';
  } else if (selectId === 'editGChannel') {
    baseOpt = '<option value="">-- Same as current channel --</option>';
  } else {
    baseOpt = '<option value="">Same as Giveaway Channel (Default)</option>';
  }

  const filtered = cachedServerChannels.filter(c => {
    if (!q) return true;
    const name = (c.name || '').toLowerCase();
    const gName = (c.guild_name || '').toLowerCase();
    const id = String(c.id || '');
    return name.includes(q) || gName.includes(q) || id.includes(q);
  });

  const optionsHtml = filtered.map(c =>
    `<option value="${c.id}">#${escapeHtml(c.name)}  •  ${escapeHtml(c.guild_name || 'Server')}</option>`
  ).join('');

  const currentVal = select.value;
  select.innerHTML = baseOpt + optionsHtml;

  if (q && filtered.length > 0) {
    select.value = filtered[0].id;
  } else if (currentVal && Array.from(select.options).some(o => o.value === currentVal)) {
    select.value = currentVal;
  }
}

// Filter & populate role select dropdowns based on search query
function filterRoleSelect(selectId, query) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const q = (query || '').trim().toLowerCase();

  if (selectId === 'gMentionRole' || selectId === 'editGMentionRole') {
    const basePings = [
      { id: '', label: 'No Ping (Silent Announcement)' },
      { id: '@everyone', label: '@everyone (Ping Entire Server)' },
      { id: '@here', label: '@here (Ping Online Members Only)' }
    ];
    const filteredBase = basePings.filter(p => !q || p.label.toLowerCase().includes(q) || p.id.toLowerCase().includes(q));
    const baseHtml = filteredBase.map(p => `<option value="${p.id}">${p.label}</option>`).join('');

    const filteredRoles = cachedServerRoles.filter(r => {
      if (!q) return true;
      const name = (r.name || '').toLowerCase();
      const gName = (r.guild_name || '').toLowerCase();
      const id = String(r.id || '');
      return name.includes(q) || gName.includes(q) || id.includes(q);
    });

    const rolesHtml = filteredRoles.map(r =>
      `<option value="${r.id}">@${escapeHtml(r.name)}  •  ${escapeHtml(r.guild_name || 'Server')}</option>`
    ).join('');

    const currentVal = select.value;
    select.innerHTML = baseHtml + (rolesHtml ? `<optgroup label="Server Roles">${rolesHtml}</optgroup>` : '');

    if (q) {
      if (filteredBase.length > 0) select.value = filteredBase[0].id;
      else if (filteredRoles.length > 0) select.value = filteredRoles[0].id;
    } else if (currentVal && Array.from(select.options).some(o => o.value === currentVal)) {
      select.value = currentVal;
    }
  } else {
    // For required role and multiplier selects
    const baseHtml = '<option value="">Select Discord Server Role...</option>';
    const filteredRoles = cachedServerRoles.filter(r => {
      if (!q) return true;
      const name = (r.name || '').toLowerCase();
      const gName = (r.guild_name || '').toLowerCase();
      const id = String(r.id || '');
      return name.includes(q) || gName.includes(q) || id.includes(q);
    });

    const rolesHtml = filteredRoles.map(r =>
      `<option value="${r.id}" data-name="${escapeHtml(r.name)}">@${escapeHtml(r.name)} (${escapeHtml(r.guild_name || 'Server')})</option>`
    ).join('');

    const currentVal = select.value;
    select.innerHTML = baseHtml + rolesHtml;

    if (q && filteredRoles.length > 0) {
      select.value = filteredRoles[0].id;
    } else if (currentVal && Array.from(select.options).some(o => o.value === currentVal)) {
      select.value = currentVal;
    }
  }
}

// Load Guild Channels for Channel Selector from API or Firebase
async function loadGuildChannels() {
  try {
    let channelArray = [];

    // 1. Try Backend API first (always returns latest live discord server channels)
    try {
      const res = await fetch(apiUrl('/api/guilds/channels'), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          channelArray = data;
        }
      }
    } catch (apiErr) {
      console.warn('Backend API channels fetch failed, trying Firebase:', apiErr);
    }

    // 2. Fallback to Firebase Cloud DB
    if (!channelArray || channelArray.length === 0) {
      const channels = await firebaseGet('channels');
      if (channels && typeof channels === 'object') {
        channelArray = Array.isArray(channels) ? channels : Object.values(channels);
      }
    }

    cachedServerChannels = channelArray;
    filterChannelSelect('gChannel', '');
    filterChannelSelect('gWinnerChannel', '');
    filterChannelSelect('editGChannel', '');
    filterChannelSelect('editGWinnerChannel', '');
  } catch (err) {
    console.error('Failed to load channels:', err);
  }
}

// Load Guild Roles for Mention Role dropdowns from API or Firebase
async function loadGuildRoles() {
  try {
    let roleArray = [];

    // 1. Try Backend API first (always returns latest live discord server roles)
    try {
      const res = await fetch(apiUrl('/api/guilds/roles'), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          roleArray = data;
        }
      }
    } catch (apiErr) {
      console.warn('Backend API roles fetch failed, trying Firebase:', apiErr);
    }

    // 2. Fallback to Firebase Cloud DB
    if (!roleArray || roleArray.length === 0) {
      const roles = await firebaseGet('roles');
      if (roles && typeof roles === 'object') {
        roleArray = Array.isArray(roles) ? roles : Object.values(roles);
      }
    }

    const uniqueRoles = [];
    const seenIds = new Set();
    roleArray.forEach(r => {
      if (r && r.id && !seenIds.has(r.id)) {
        seenIds.add(r.id);
        uniqueRoles.push(r);
      }
    });

    cachedServerRoles = uniqueRoles.filter(r => r.id !== '@everyone');
    filterRoleSelect('gMentionRole', '');
    filterRoleSelect('editGMentionRole', '');
    filterRoleSelect('gReqRoleSelect', '');
    filterRoleSelect('editGReqRoleSelect', '');
    filterRoleSelect('gRoleMultSelect', '');
    filterRoleSelect('editGRoleMultSelect', '');
  } catch (err) {
    console.error('Failed to load roles:', err);
  }
}

// -------- Required Role Chip Management (OR Logic) -------- //
let createRequiredRoles = [];
let editRequiredRoles = [];

function renderCreateRequiredRoles() {
  const container = document.getElementById('gReqRolesList');
  if (!container) return;
  if (!createRequiredRoles.length) {
    container.innerHTML = `<span style="font-size: 0.8rem; color: var(--text-muted); font-style: italic;">No required roles selected (Open to everyone)</span>`;
    return;
  }
  container.innerHTML = createRequiredRoles.map((role, idx) => `
    <span class="role-badge-chip">
      @${escapeHtml(role.name || role.id)}
      <span class="remove-btn" onclick="removeRequiredRole(${idx})" title="Remove role">×</span>
    </span>
  `).join('');
}

function addSelectedRequiredRole() {
  const sel = document.getElementById('gReqRoleSelect');
  if (!sel || !sel.value) return;
  const opt = sel.options[sel.selectedIndex];
  const roleId = sel.value;
  const roleName = opt.getAttribute('data-name') || opt.text.replace(/^@/, '').split(' (')[0];
  if (!createRequiredRoles.some(r => r.id === roleId)) {
    createRequiredRoles.push({ id: roleId, name: roleName });
    renderCreateRequiredRoles();
  }
  sel.value = '';
  const searchInp = document.getElementById('gReqRoleSearch');
  if (searchInp) searchInp.value = '';
  filterRoleSelect('gReqRoleSelect', '');
}

function addManualRequiredRole() {
  const inp = document.getElementById('gReqRoleManual');
  if (!inp || !inp.value.trim()) return;
  const val = inp.value.trim();
  if (!createRequiredRoles.some(r => r.id === val || r.name.toLowerCase() === val.toLowerCase())) {
    createRequiredRoles.push({ id: val, name: val });
    renderCreateRequiredRoles();
  }
  inp.value = '';
}

function removeRequiredRole(index) {
  if (index >= 0 && index < createRequiredRoles.length) {
    createRequiredRoles.splice(index, 1);
    renderCreateRequiredRoles();
  }
}

function renderEditRequiredRoles() {
  const container = document.getElementById('editGReqRolesList');
  if (!container) return;
  if (!editRequiredRoles.length) {
    container.innerHTML = `<span style="font-size: 0.8rem; color: var(--text-muted); font-style: italic;">No required roles selected (Open to everyone)</span>`;
    return;
  }
  container.innerHTML = editRequiredRoles.map((role, idx) => `
    <span class="role-badge-chip">
      @${escapeHtml(role.name || role.id)}
      <span class="remove-btn" onclick="removeEditRequiredRole(${idx})" title="Remove role">×</span>
    </span>
  `).join('');
}

function addEditSelectedRequiredRole() {
  const sel = document.getElementById('editGReqRoleSelect');
  if (!sel || !sel.value) return;
  const opt = sel.options[sel.selectedIndex];
  const roleId = sel.value;
  const roleName = opt.getAttribute('data-name') || opt.text.replace(/^@/, '').split(' (')[0];
  if (!editRequiredRoles.some(r => r.id === roleId)) {
    editRequiredRoles.push({ id: roleId, name: roleName });
    renderEditRequiredRoles();
  }
  sel.value = '';
  const searchInp = document.getElementById('editGReqRoleSearch');
  if (searchInp) searchInp.value = '';
  filterRoleSelect('editGReqRoleSelect', '');
}

function addEditManualRequiredRole() {
  const inp = document.getElementById('editGReqRoleManual');
  if (!inp || !inp.value.trim()) return;
  const val = inp.value.trim();
  if (!editRequiredRoles.some(r => r.id === val || r.name.toLowerCase() === val.toLowerCase())) {
    editRequiredRoles.push({ id: val, name: val });
    renderEditRequiredRoles();
  }
  inp.value = '';
}

function removeEditRequiredRole(index) {
  if (index >= 0 && index < editRequiredRoles.length) {
    editRequiredRoles.splice(index, 1);
    renderEditRequiredRoles();
  }
}

// -------- Role-Based Extra Entries / Multipliers Management -------- //
let createRoleMultipliers = [];
let editRoleMultipliers = [];

function renderCreateRoleMultipliers() {
  const container = document.getElementById('gRoleMultsList');
  if (!container) return;
  if (!createRoleMultipliers.length) {
    container.innerHTML = `<span style="font-size: 0.8rem; color: var(--text-muted); font-style: italic;">No role multipliers configured (Standard 1x entry for everyone)</span>`;
    return;
  }
  container.innerHTML = createRoleMultipliers.map((rm, idx) => `
    <span class="role-badge-chip" style="background: rgba(234, 179, 8, 0.15); border-color: rgba(234, 179, 8, 0.35); color: #fde047;">
      @${escapeHtml(rm.name || rm.id)} — <b>${rm.multiplier}x ${rm.multiplier === 1 ? 'Entry' : 'Entries'}</b>
      <span class="remove-btn" onclick="removeRoleMultiplier(${idx})" title="Remove multiplier">×</span>
    </span>
  `).join('');
}

function addRoleMultiplier() {
  const sel = document.getElementById('gRoleMultSelect');
  const manual = document.getElementById('gRoleMultManual');
  const countInp = document.getElementById('gRoleMultCount');
  const count = parseInt(countInp ? countInp.value : 2) || 1;

  let roleId = '';
  let roleName = '';

  if (sel && sel.value) {
    roleId = sel.value;
    const opt = sel.options[sel.selectedIndex];
    roleName = opt.getAttribute('data-name') || opt.text.replace(/^@/, '').split(' (')[0];
    sel.value = '';
  } else if (manual && manual.value.trim()) {
    roleId = manual.value.trim();
    roleName = roleId;
    manual.value = '';
  } else {
    showToast('Please select or type a Discord role', 'info');
    return;
  }

  const existingIdx = createRoleMultipliers.findIndex(r => r.id === roleId || r.name.toLowerCase() === roleName.toLowerCase());
  if (existingIdx >= 0) {
    createRoleMultipliers[existingIdx].multiplier = count;
  } else {
    createRoleMultipliers.push({ id: roleId, name: roleName, multiplier: count });
  }
  renderCreateRoleMultipliers();
  const searchInp = document.getElementById('gRoleMultSearch');
  if (searchInp) searchInp.value = '';
  filterRoleSelect('gRoleMultSelect', '');
}

function removeRoleMultiplier(idx) {
  if (idx >= 0 && idx < createRoleMultipliers.length) {
    createRoleMultipliers.splice(idx, 1);
    renderCreateRoleMultipliers();
  }
}

function renderEditRoleMultipliers() {
  const container = document.getElementById('editGRoleMultsList');
  if (!container) return;
  if (!editRoleMultipliers.length) {
    container.innerHTML = `<span style="font-size: 0.8rem; color: var(--text-muted); font-style: italic;">No role multipliers configured (Standard 1x entry for everyone)</span>`;
    return;
  }
  container.innerHTML = editRoleMultipliers.map((rm, idx) => `
    <span class="role-badge-chip" style="background: rgba(234, 179, 8, 0.15); border-color: rgba(234, 179, 8, 0.35); color: #fde047;">
      @${escapeHtml(rm.name || rm.id)} — <b>${rm.multiplier}x ${rm.multiplier === 1 ? 'Entry' : 'Entries'}</b>
      <span class="remove-btn" onclick="removeEditRoleMultiplier(${idx})" title="Remove multiplier">×</span>
    </span>
  `).join('');
}

function addEditRoleMultiplier() {
  const sel = document.getElementById('editGRoleMultSelect');
  const manual = document.getElementById('editGRoleMultManual');
  const countInp = document.getElementById('editGRoleMultCount');
  const count = parseInt(countInp ? countInp.value : 2) || 1;

  let roleId = '';
  let roleName = '';

  if (sel && sel.value) {
    roleId = sel.value;
    const opt = sel.options[sel.selectedIndex];
    roleName = opt.getAttribute('data-name') || opt.text.replace(/^@/, '').split(' (')[0];
    sel.value = '';
  } else if (manual && manual.value.trim()) {
    roleId = manual.value.trim();
    roleName = roleId;
    manual.value = '';
  } else {
    showToast('Please select or type a Discord role', 'info');
    return;
  }

  const existingIdx = editRoleMultipliers.findIndex(r => r.id === roleId || r.name.toLowerCase() === roleName.toLowerCase());
  if (existingIdx >= 0) {
    editRoleMultipliers[existingIdx].multiplier = count;
  } else {
    editRoleMultipliers.push({ id: roleId, name: roleName, multiplier: count });
  }
  renderEditRoleMultipliers();
}

function removeEditRoleMultiplier(idx) {
  if (idx >= 0 && idx < editRoleMultipliers.length) {
    editRoleMultipliers.splice(idx, 1);
    renderEditRoleMultipliers();
  }
}



// Check Authentication (localStorage-based)
function checkAuth() {
  const authContainer = document.getElementById('authContainer');
  const createBtn = document.getElementById('createGiveawayBtn');
  const saved = localStorage.getItem('arcie_admin');

  if (saved) {
    try {
      currentUser = JSON.parse(saved);
      currentUser.is_admin = true;

      authContainer.innerHTML = `
        <div class="user-pill" style="cursor:pointer;" onclick="adminLogout()">
          <span class="user-name">${escapeHtml(currentUser.username || 'Admin')}</span>
          <span class="admin-badge">ADMIN</span>
        </div>
      `;
      createBtn.style.display = 'inline-flex';
    } catch (e) {
      localStorage.removeItem('arcie_admin');
      currentUser = null;
    }
  }

  if (!currentUser) {
    createBtn.style.display = 'none';
    authContainer.innerHTML = `
      <button class="btn btn-purple" onclick="openModal('passLoginModal')">
        Admin Sign In
      </button>
    `;
  }

  // Show/hide admin-only tabs & backup buttons
  const isAdmin = !!(currentUser && currentUser.is_admin);
  document.querySelectorAll('.admin-only-tab').forEach(tab => {
    tab.style.display = isAdmin ? '' : 'none';
  });

  const dlBtn = document.getElementById('downloadBackupBtn');
  const rtBtn = document.getElementById('restoreBackupBtn');
  if (dlBtn) dlBtn.style.display = isAdmin ? 'inline-flex' : 'none';
  if (rtBtn) rtBtn.style.display = isAdmin ? 'inline-flex' : 'none';
}

// Download Backup JSON
async function downloadBackup() {
  showToast('⏳ Generating database backup...', 'info');
  try {
    let backupData = null;

    // 1. Try Backend API endpoint
    try {
      const res = await fetch(apiUrl('/api/admin/backup'), { credentials: 'include' });
      if (res.ok) {
        backupData = await res.json();
      }
    } catch (e) {
      console.warn('Backend backup API unavailable, trying direct Firebase export:', e);
    }

    // 2. Fallback to direct Firebase export (Vercel / static mode)
    if (!backupData) {
      const gData = await firebaseGet('giveaways') || {};
      const eData = await firebaseGet('giveaway_entries') || {};
      const pData = await firebaseGet('user_profiles') || {};
      const rData = await firebaseGet('reaction_roles') || {};

      backupData = {
        version: "1.0",
        backup_timestamp: new Date().toISOString(),
        giveaways: typeof gData === 'object' ? gData : {},
        giveaway_entries: typeof eData === 'object' ? eData : {},
        user_profiles: typeof pData === 'object' ? pData : {},
        reaction_roles: typeof rData === 'object' ? rData : {}
      };
    }

    const str = JSON.stringify(backupData, null, 2);
    const blob = new Blob([str], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `arcie_bot_backup_${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Backup downloaded successfully', 'success');
  } catch (err) {
    console.error('Backup download error:', err);
    showToast('Failed to download backup: ' + err.message, 'error');
  }
}

// Restore Backup JSON
async function handleRestoreBackup(input) {
  const file = input.files && input.files[0];
  if (!file) return;

  if (!confirm('WARNING: Restoring a backup will overwrite existing giveaways, participant entries, and user profiles.\n\nAre you sure you want to proceed?')) {
    input.value = '';
    return;
  }

  showToast('⏳ Restoring database backup...', 'info');

  const reader = new FileReader();
  reader.onload = async function (e) {
    try {
      const data = JSON.parse(e.target.result);
      if (!data || typeof data !== 'object') {
        throw new Error('Invalid JSON file');
      }

      let restored = false;

      // 1. Try Backend API endpoint first
      try {
        const res = await fetch(apiUrl('/api/admin/restore'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify(data)
        });
        if (res.ok) {
          const resData = await res.json();
          if (resData.success) {
            restored = true;
          }
        }
      } catch (e) {
        console.warn('Backend restore API unavailable, trying direct Firebase restore:', e);
      }

      // 2. Direct Firebase sync/restore fallback (Vercel / static mode)
      if (!restored) {
        if (data.giveaways && typeof data.giveaways === 'object') {
          const existingG = await firebaseGet('giveaways') || {};
          const mergedG = Object.assign({}, existingG, data.giveaways);
          await firebasePut('giveaways', mergedG);
        }
        if (data.giveaway_entries && typeof data.giveaway_entries === 'object') {
          const existingE = await firebaseGet('giveaway_entries') || {};
          const mergedE = Object.assign({}, existingE, data.giveaway_entries);
          await firebasePut('giveaway_entries', mergedE);
        }
        if (data.user_profiles && typeof data.user_profiles === 'object') {
          const existingP = await firebaseGet('user_profiles') || {};
          const mergedP = Object.assign({}, existingP, data.user_profiles);
          await firebasePut('user_profiles', mergedP);
        }
        if (data.reaction_roles && typeof data.reaction_roles === 'object') {
          const existingR = await firebaseGet('reaction_roles') || {};
          const mergedR = Object.assign({}, existingR, data.reaction_roles);
          await firebasePut('reaction_roles', mergedR);
        }
        restored = true;
      }

      showToast('Backup restored successfully', 'success');
      await loadGiveaways();
    } catch (err) {
      console.error('Restore error:', err);
      showToast('Failed to restore backup: ' + err.message, 'error');
    } finally {
      input.value = '';
    }
  };
  reader.readAsText(file);
}

// Admin Password Login
async function submitPasswordLogin(e) {
  e.preventDefault();
  const username = document.getElementById('passUser').value.trim();
  const password = document.getElementById('passWord').value.trim();

  if (password !== 'innercirclefcfs78@1' && password !== 'innercircle78@1') {
    showToast('Invalid admin password', 'error');
    return;
  }

  currentUser = {
    id: 'admin_' + Date.now(),
    username: username || 'Admin',
    is_admin: true
  };
  localStorage.setItem('arcie_admin', JSON.stringify(currentUser));
  showToast('Signed in as Admin', 'success');
  closeModal('passLoginModal');
  checkAuth();
}

// Admin Logout
function adminLogout() {
  if (confirm('Sign out?')) {
    localStorage.removeItem('arcie_admin');
    currentUser = null;
    checkAuth();
    showToast('Signed out', 'info');
  }
}

// Load Giveaways from API first with Firebase fallback
async function loadGiveaways() {
  try {
    let data = null;

    // 1. Try backend API first (zero Firebase bandwidth)
    try {
      const res = await fetch(apiUrl('/api/giveaways'), { credentials: 'include' });
      if (res.ok) {
        const apiData = await res.json();
        if (Array.isArray(apiData) && apiData.length > 0) {
          currentGiveaways = apiData;
          updateHeroStats();
          renderGiveaways();
          return;
        }
      }
    } catch (apiErr) {
      console.warn('Backend API giveaways fetch failed, attempting Firebase fallback:', apiErr);
    }

    // 2. Fallback to Firebase Cloud DB
    try {
      data = await firebaseGet('giveaways');
    } catch (e) {
      console.warn('Firebase giveaways fetch failed:', e);
    }

    if (data && typeof data === 'object') {
      currentGiveaways = Object.values(data);
    } else {
      currentGiveaways = [];
    }
    updateHeroStats();
    renderGiveaways();
  } catch (err) {
    console.error('Failed to load giveaways:', err);
    showToast('Failed to load giveaways', 'error');
  }
}

function updateHeroStats() {
  // Public hero stats and spots counters removed per UI design
}

// Render Giveaway Cards (Tessera Architecture)
function renderGiveaways(highlightedGiveaway = null) {
  const grid = document.getElementById('giveawayGrid');
  const countBadge = document.getElementById('giveawayCountBadge');
  const now = Math.floor(Date.now() / 1000);
  const isAdmin = currentUser && currentUser.is_admin;

  let filtered = currentGiveaways;

  if (highlightedGiveaway && highlightedGiveaway.id) {
    const otherGiveaways = currentGiveaways.filter(g => g.id !== highlightedGiveaway.id);
    if (!isAdmin) {
      const activeOthers = otherGiveaways.filter(g => g.is_active && g.ends_at > now);
      activeOthers.sort((a, b) => (Number(a.ends_at) || 0) - (Number(b.ends_at) || 0));
      filtered = [highlightedGiveaway, ...activeOthers];
    } else {
      otherGiveaways.sort((a, b) => {
        const timeA = Number(a.ends_at || a.created_at || 0);
        const timeB = Number(b.ends_at || b.created_at || 0);
        return timeB - timeA;
      });
      filtered = [highlightedGiveaway, ...otherGiveaways];
    }
  } else {
    if (!isAdmin) {
      filtered = currentGiveaways.filter(g => g.is_active && g.ends_at > now);
      filtered.sort((a, b) => (Number(a.ends_at) || 0) - (Number(b.ends_at) || 0));
    } else {
      if (currentFilter === 'active') {
        filtered = currentGiveaways.filter(g => g.is_active && g.ends_at > now);
        filtered.sort((a, b) => (Number(a.ends_at) || 0) - (Number(b.ends_at) || 0));
      } else if (currentFilter === 'ended') {
        filtered = currentGiveaways.filter(g => !g.is_active || g.ends_at <= now);
        filtered.sort((a, b) => {
          const timeA = Number(a.ends_at || a.created_at || 0);
          const timeB = Number(b.ends_at || b.created_at || 0);
          return timeB - timeA;
        });
      } else {
        const activeList = currentGiveaways.filter(g => g.is_active && g.ends_at > now);
        activeList.sort((a, b) => (Number(a.ends_at) || 0) - (Number(b.ends_at) || 0));
        const endedList = currentGiveaways.filter(g => !g.is_active || g.ends_at <= now);
        endedList.sort((a, b) => {
          const timeA = Number(a.ends_at || a.created_at || 0);
          const timeB = Number(b.ends_at || b.created_at || 0);
          return timeB - timeA;
        });
        filtered = [...activeList, ...endedList];
      }
    }
  }

  if (countBadge) {
    countBadge.innerText = `${filtered.length} ${currentFilter === 'active' ? 'active' : 'total'}`;
  }

  if (filtered.length === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon-box">
          ${svgClock()}
        </div>
        <p class="empty-title">No Raffles Found</p>
        <p class="empty-hint">There are no active raffles in this category.</p>
      </div>
    `;
    return;
  }

  grid.innerHTML = filtered.map(g => {
    const isEnded = !g.is_active || g.ends_at <= now;
    const timeLeft = getTimeLeftString(g.ends_at);

    // Calculate spots
    let spotCount = 0;
    if (g.spot_tiers && g.spot_tiers.length) {
      spotCount = g.spot_tiers.reduce((acc, t) => acc + (parseInt(t.count) || 0), 0);
    } else {
      spotCount = (g.guaranteed_spots || 0) + (g.fcfs_spots || 0);
    }
    if (!spotCount) spotCount = 1;

    // Requirement tags
    const reqBadges = [];
    if (g.tasks?.twitter_follow) reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>Follow @${escapeHtml(g.tasks.twitter_follow)}<span class="bracket">]</span></span>`);
    if (g.tasks?.twitter_like) reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>Like<span class="bracket">]</span></span>`);
    if (g.tasks?.twitter_retweet) reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>Retweet<span class="bracket">]</span></span>`);
    if (g.tasks?.discord_join) reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>Discord<span class="bracket">]</span></span>`);
    if (g.tasks?.roles?.length) reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>Role: ${escapeHtml(g.tasks.roles[0])}<span class="bracket">]</span></span>`);
    if (g.tasks?.dynamic_tasks && g.tasks.dynamic_tasks.length) {
      g.tasks.dynamic_tasks.slice(0, 2).forEach(dt => {
        reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>${escapeHtml(dt.value)}<span class="bracket">]</span></span>`);
      });
    }
    if (g.tasks?.require_evm) reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>EVM<span class="bracket">]</span></span>`);
    if (g.tasks?.require_solana) reqBadges.push(`<span class="g-tag"><span class="bracket">[</span>Solana<span class="bracket">]</span></span>`);

    let statusHtml = '';
    if (g.is_done) {
      statusHtml = `<span class="g-card-status-tag status-done">${svgShield()} Sheet Locked</span>`;
    } else if (isEnded) {
      statusHtml = `<span class="g-card-status-tag status-ended">${svgClock()} Ended</span>`;
    } else {
      statusHtml = `<span class="g-card-status-tag status-live"><span class="live-dot" style="margin-right:2px;"></span>Live</span>`;
    }

    return `
      <div class="g-card">
        <div class="g-card-banner-wrap">
          ${statusHtml}
          ${g.banner_url ? `<img src="${escapeHtml(g.banner_url)}" class="g-card-banner" alt="banner" onerror="this.parentElement.style.display='none'">` : '<div class="g-card-banner-fallback"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect width="18" height="18" x="3" y="3" rx="2"></rect><circle cx="9" cy="9" r="2"></circle><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"></path></svg></div>'}
          <div class="g-card-banner-overlay"></div>
        </div>

        <div class="g-card-body">
          <div class="g-card-host-row">
            <span class="g-card-host">by <strong>${escapeHtml(g.hosted_by || 'Admin')}</strong></span>
            <span class="g-card-network-badge">${escapeHtml(g.network || 'Ethereum')}</span>
          </div>

          <h3 class="g-card-title">${escapeHtml(g.title)}</h3>
          <div class="g-card-desc">${formatMarkdownDescription(g.description)}</div>

          <div class="g-metrics-bar">
            <div class="g-metric-cell">
              <span class="g-metric-label">${svgUsers()} Entries</span>
              <span class="g-metric-value tabular-nums">${g.entries_count || 0}</span>
            </div>
            <div class="g-metric-cell">
              <span class="g-metric-label">${svgTrophy()} Winners</span>
              <span class="g-metric-value tabular-nums">${spotCount}×</span>
            </div>
            <div class="g-metric-cell">
              <span class="g-metric-label">${svgClock()} Closes</span>
              <span class="g-metric-value font-mono" style="font-size:0.75rem;">${isEnded ? 'Closed' : timeLeft}</span>
            </div>
          </div>

          <div class="g-card-tags">
            ${reqBadges.slice(0, 3).join('')}
            ${reqBadges.length > 3 ? `<span class="g-tag font-mono">+${reqBadges.length - 3}</span>` : ''}
          </div>
        </div>

        <div class="g-card-footer">
          <div style="font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-muted); display: inline-flex; align-items: center; gap: 4px;">
            ${svgUsers()} <span class="tabular-nums">${g.entries_count || 0}</span> entered
          </div>
          <div class="g-footer-actions">
            <button type="button" class="btn btn-outline btn-sm btn-icon" onclick="copyShareLink('${g.id}')" title="Copy Share Link">
              ${svgShare()}
            </button>
            ${isAdmin ? `<button type="button" class="btn btn-danger btn-sm btn-icon" onclick="deleteGiveaway('${g.id}')" title="Delete Giveaway">${svgTrash()}</button>` : ''}
            <button class="btn btn-primary btn-sm" onclick="openDetailModal('${g.id}')">
              <span>${isEnded ? 'Results' : 'Enter'}</span>
              <span style="font-family: var(--font-mono); font-size: 0.8rem; margin-left: 2px;">→</span>
            </button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}



let spotTierCount = 0;

function addSpotTier(defaultName = '', defaultCount = 1) {
  const container = document.getElementById('spotTiersList');
  if (!container) return;

  spotTierCount++;
  const id = `spot_tier_${spotTierCount}`;
  const div = document.createElement('div');
  div.id = id;
  div.style.display = 'flex';
  div.style.gap = '8px';
  div.style.alignItems = 'center';
  div.style.background = 'rgba(0,0,0,0.2)';
  div.style.padding = '6px 10px';
  div.style.borderRadius = 'var(--radius-sm)';
  div.style.border = '1px solid var(--border-color)';

  div.innerHTML = `
    <input type="text" class="form-input spot-tier-name" value="${escapeHtml(defaultName)}" placeholder="Tier Name (e.g. GTD, FCFS, VIP)" style="flex: 2; padding: 6px 10px; font-size: 0.85rem;">
    <input type="number" class="form-input spot-tier-count" value="${defaultCount}" min="1" placeholder="Spots" style="flex: 1; padding: 6px 10px; font-size: 0.85rem;">
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">${svgTrash()}</button>
  `;

  container.appendChild(div);
}

function getSpotTiersPayload() {
  const tiers = [];
  document.querySelectorAll('#spotTiersList > div').forEach(row => {
    const nameInput = row.querySelector('.spot-tier-name');
    const countInput = row.querySelector('.spot-tier-count');
    if (nameInput && countInput) {
      const name = nameInput.value.trim();
      const count = parseInt(countInput.value) || 0;
      if (name && count > 0) {
        tiers.push({ name, count });
      }
    }
  });
  return tiers;
}

let dynamicTaskCount = 0;

function addDynamicTask(type, defaultVal = '') {
  const container = document.getElementById('dynamicTasksList');
  if (!container) return;

  dynamicTaskCount++;
  const id = `task_item_${dynamicTaskCount}`;
  const div = document.createElement('div');
  div.className = 'task-builder-item';
  div.id = id;
  div.style.display = 'flex';
  div.style.gap = '8px';
  div.style.alignItems = 'center';
  div.style.background = 'rgba(0,0,0,0.2)';
  div.style.padding = '8px 12px';
  div.style.borderRadius = 'var(--radius-sm)';
  div.style.border = '1px solid var(--border-color)';

  let typeBadge = '';
  let placeholder = '';

  if (type === 'twitter_follow') {
    typeBadge = 'Follow';
    placeholder = 'Handle (e.g. @WizardX_0x)';
  } else if (type === 'twitter_like') {
    typeBadge = 'Like';
    placeholder = 'Tweet Link / URL';
  } else if (type === 'twitter_retweet') {
    typeBadge = 'Retweet';
    placeholder = 'Tweet Link / URL';
  } else if (type === 'twitter_comment') {
    typeBadge = 'Comment';
    placeholder = 'Tweet Link / URL to Comment';
  } else if (type === 'discord_join' || type === 'discord_server') {
    typeBadge = 'Discord';
    placeholder = 'https://discord.gg/invitecode or Server Name';
  } else if (type === 'tiktok_follow') {
    typeBadge = 'TikTok';
    placeholder = 'TikTok Handle / Link';
  } else if (type === 'youtube_follow') {
    typeBadge = 'YouTube';
    placeholder = 'Channel Link / Name';
  } else if (type === 'role_require') {
    typeBadge = 'Role';
    placeholder = 'Required Server Role Name';
  } else {
    typeBadge = 'Custom';
    placeholder = 'Task instructions...';
  }

  div.innerHTML = `
    <span class="g-badge g-badge-fcfs" style="min-width: 90px; text-align: center;">${typeBadge}</span>
    <input type="text" class="form-input dynamic-task-val" data-type="${type}" value="${escapeHtml(defaultVal)}" placeholder="${placeholder}" style="flex: 1; padding: 6px 10px; font-size: 0.85rem;">
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">${svgTrash()}</button>
  `;

  container.appendChild(div);
}

function getDynamicTasksPayload() {
  const tasks = [];
  document.querySelectorAll('.dynamic-task-val').forEach(input => {
    const val = input.value.trim();
    const type = input.dataset.type;
    if (val) {
      tasks.push({ type, value: val });
    }
  });
  return tasks;
}

// Helper to handle banner image file uploads (saves to backend upload folder or Data URL fallback)
async function handleBannerFileUpload(inputElement, targetUrlInputId, previewContainerId) {
  const file = inputElement.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('image', file);

  showToast('Uploading image...', 'info');
  try {
    const res = await fetch(apiUrl('/api/upload'), {
      method: 'POST',
      credentials: 'include',
      body: formData
    });
    const data = await res.json();
    if (res.ok && data.url) {
      document.getElementById(targetUrlInputId).value = data.url;
      const previewBox = document.getElementById(previewContainerId);
      if (previewBox) {
        previewBox.style.display = 'block';
        previewBox.querySelector('img').src = data.url;
      }
      showToast('Banner image uploaded successfully', 'success');
      return;
    }
  } catch (err) {
    console.warn('Backend upload API unavailable, using local file reader preview:', err);
  }

  // Local fallback: read file as Data URL
  const reader = new FileReader();
  reader.onload = (e) => {
    const dataUrl = e.target.result;
    document.getElementById(targetUrlInputId).value = dataUrl;
    const previewBox = document.getElementById(previewContainerId);
    if (previewBox) {
      previewBox.style.display = 'block';
      previewBox.querySelector('img').src = dataUrl;
    }
    showToast('Image loaded successfully', 'success');
  };
  reader.readAsDataURL(file);
}

// Submit Create Giveaway (Calls backend API so Discord announcement embed posts IMMEDIATELY)
let isSubmittingCreate = false;
async function submitCreateGiveaway() {
  if (isSubmittingCreate) return;

  const btn = document.getElementById('publishBtn');
  if (btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.innerHTML = 'Publishing...';
  }
  isSubmittingCreate = true;

  try {
    const title = document.getElementById('gTitle').value.trim();
    const description = document.getElementById('gDesc').value.trim();
    const banner_url = document.getElementById('gBanner').value.trim();
    const channelSelect = document.getElementById('gChannel').value;
    const channelManual = document.getElementById('gChannelManual') ? document.getElementById('gChannelManual').value.trim() : '';
    const channel_id = channelManual || channelSelect || 'auto';

    const mention_role = document.getElementById('gMentionRole') ? document.getElementById('gMentionRole').value : '';
    const winnerChannelSelect = document.getElementById('gWinnerChannel') ? document.getElementById('gWinnerChannel').value : '';
    const winnerChannelManual = document.getElementById('gWinnerChannelManual') ? document.getElementById('gWinnerChannelManual').value.trim() : '';
    const winner_channel_id = winnerChannelManual || winnerChannelSelect || '';

    if (!title || !description) {
      showToast('Please fill in Title and Description', 'error');
      return;
    }
    const spot_tiers = getSpotTiersPayload();
    const min_per_user = parseInt(document.getElementById('gMinPerUser').value) || 1;
    const max_per_user = parseInt(document.getElementById('gMaxPerUser').value) || 1;
    const duration_val = parseFloat(document.getElementById('gDurationVal').value) || 15;
    const duration_unit = document.getElementById('gDurationUnit').value;
    const network = document.getElementById('gNetwork').value.trim() || 'Ethereum';

    const dynamic_tasks = getDynamicTasksPayload();
    const require_evm = document.getElementById('reqEvm').checked;
    const require_solana = document.getElementById('reqSolana').checked;

    const twitter_link = document.getElementById('gTwitterLink')?.value.trim() || '';
    const discord_link = document.getElementById('gDiscordLink')?.value.trim() || '';
    const telegram_link = document.getElementById('gTelegramLink')?.value.trim() || '';
    const website_link = document.getElementById('gWebsiteLink')?.value.trim() || '';
    const social_links = { twitter_link, discord_link, telegram_link, website_link };

    const giveawayId = 'g_' + Date.now();
    let durationInSeconds = duration_val * 60;
    if (duration_unit === 'hours') durationInSeconds = duration_val * 3600;
    if (duration_unit === 'days') durationInSeconds = duration_val * 86400;

    const selectedRoles = createRequiredRoles.map(r => r.id);

    const giveawayObj = {
      id: giveawayId,
      title,
      description,
      banner_url,
      channel_id: channel_id || 'general',
      winner_channel_id,
      mention_role,
      spot_tiers,
      min_per_user,
      max_per_user,
      duration_val,
      duration_unit,
      duration_hours: duration_val,
      network,
      social_links,
      is_active: true,
      created_at: Math.floor(Date.now() / 1000),
      ends_at: Math.floor(Date.now() / 1000) + durationInSeconds,
      hosted_by: currentUser ? currentUser.username : 'Admin',
      guaranteed_spots: (spot_tiers.find(t => t.name?.toLowerCase().includes('guarantee') || t.name === 'GTD') || {}).count || 0,
      fcfs_spots: (spot_tiers.find(t => t.name?.toLowerCase().includes('fcfs')) || {}).count || 0,
      entries_count: 0,
      role_multipliers: createRoleMultipliers,
      tasks: {
        dynamic_tasks,
        require_evm,
        require_solana,
        roles: selectedRoles
      }
    };

    try {
      // 1. Post to Backend Bot Server so Discord announcement embed is sent IN REAL-TIME
      const res = await fetch(apiUrl('/api/giveaways'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(giveawayObj)
      });
      
      if (res.ok) {
        const created = await res.json().catch(() => null);
        const finalId = (created && created.id) ? created.id : giveawayId;
        await firebasePut('giveaway_entries/' + finalId, []);
        showToast('Giveaway published & posted to Discord', 'success');
      } else {
        // Fallback for static/offline mode only if backend is unreachable
        const errData = await res.json().catch(() => ({}));
        if (res.status === 403) {
          showToast(errData.error || 'Admin permission required', 'error');
          return;
        }
        await firebasePut('giveaways/' + giveawayId, giveawayObj);
        await firebasePut('giveaway_entries/' + giveawayId, []);
        showToast('Giveaway created (Cloud DB sync)', 'success');
      }
    } catch (err) {
      console.warn('Backend API create error, using direct Cloud DB sync:', err);
      await firebasePut('giveaways/' + giveawayId, giveawayObj);
      await firebasePut('giveaway_entries/' + giveawayId, []);
      showToast('Giveaway created (Cloud DB sync)', 'success');
    }

    closeModal('createModal');
    createRequiredRoles = [];
    renderCreateRequiredRoles();
    createRoleMultipliers = [];
    renderCreateRoleMultipliers();
    resetCreateForm();
    await loadGiveaways();
  } finally {
    isSubmittingCreate = false;
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = 'Publish Giveaway';
    }
  }
}

// Edit Giveaway Functions
let editSpotTierCount = 0;
function addEditSpotTier(defaultName = '', defaultCount = 1) {
  const container = document.getElementById('editSpotTiersList');
  if (!container) return;

  editSpotTierCount++;
  const id = `edit_spot_tier_${editSpotTierCount}`;
  const div = document.createElement('div');
  div.id = id;
  div.className = 'spot-tier-row';
  div.style.display = 'flex';
  div.style.gap = '8px';
  div.style.alignItems = 'center';
  div.style.background = 'rgba(0,0,0,0.2)';
  div.style.padding = '6px 10px';
  div.style.borderRadius = 'var(--radius-sm)';
  div.style.border = '1px solid var(--border-color)';

  div.innerHTML = `
    <input type="text" class="form-input edit-spot-tier-name" value="${escapeHtml(defaultName)}" placeholder="Tier Name" style="flex: 2; padding: 6px 10px; font-size: 0.85rem;">
    <input type="number" class="form-input edit-spot-tier-count" value="${defaultCount}" min="1" placeholder="Spots" style="flex: 1; padding: 6px 10px; font-size: 0.85rem;">
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">${svgTrash()}</button>
  `;

  container.appendChild(div);
}

function getEditSpotTiersPayload() {
  const tiers = [];
  document.querySelectorAll('#editSpotTiersList .spot-tier-row').forEach(row => {
    const nameInput = row.querySelector('.edit-spot-tier-name');
    const countInput = row.querySelector('.edit-spot-tier-count');
    if (nameInput && countInput) {
      const name = nameInput.value.trim();
      const count = parseInt(countInput.value) || 0;
      if (name && count > 0) {
        tiers.push({ name, count });
      }
    }
  });
  return tiers;
}

let editDynamicTaskCount = 0;
function addEditDynamicTask(type, defaultVal = '') {
  const container = document.getElementById('editDynamicTasksList');
  if (!container) return;

  editDynamicTaskCount++;
  const id = `edit_task_item_${editDynamicTaskCount}`;
  const div = document.createElement('div');
  div.className = 'dynamic-task-row';
  div.id = id;
  div.style.display = 'flex';
  div.style.gap = '8px';
  div.style.alignItems = 'center';
  div.style.background = 'rgba(0,0,0,0.2)';
  div.style.padding = '8px 12px';
  div.style.borderRadius = 'var(--radius-sm)';
  div.style.border = '1px solid var(--border-color)';

  let typeBadge = type;
  if (type === 'twitter_follow') typeBadge = 'Follow';
  else if (type === 'twitter_like') typeBadge = 'Like';
  else if (type === 'twitter_retweet') typeBadge = 'Retweet';
  else if (type === 'twitter_comment') typeBadge = 'Comment';
  else if (type === 'discord_join' || type === 'discord_server') typeBadge = 'Discord';
  else if (type === 'tiktok_follow') typeBadge = 'TikTok';
  else if (type === 'youtube_follow') typeBadge = 'YouTube';
  else if (type === 'role_require') typeBadge = 'Role';
  else typeBadge = 'Custom';

  div.innerHTML = `
    <span class="g-badge g-badge-fcfs" style="min-width: 90px; text-align: center;">${typeBadge}</span>
    <input type="text" class="form-input edit-dynamic-task-val" data-type="${type}" value="${escapeHtml(defaultVal)}" placeholder="Requirement value..." style="flex: 1; padding: 6px 10px; font-size: 0.85rem;">
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">${svgTrash()}</button>
  `;

  container.appendChild(div);
}

function getEditDynamicTasksPayload() {
  const tasks = [];
  document.querySelectorAll('.edit-dynamic-task-val').forEach(input => {
    const val = input.value.trim();
    const type = input.dataset.type;
    if (val) {
      tasks.push({ type, value: val });
    }
  });
  return tasks;
}

function openEditModal(giveawayId) {
  const g = currentGiveaways.find(x => x.id === giveawayId);
  if (!g) return;

  // Clear search inputs and reset options for edit modal
  const editModal = document.getElementById('editModal');
  if (editModal) {
    editModal.querySelectorAll('.select-search-input').forEach(i => i.value = '');
  }
  filterChannelSelect('editGChannel', '');
  filterChannelSelect('editGWinnerChannel', '');
  filterRoleSelect('editGMentionRole', '');
  filterRoleSelect('editGReqRoleSelect', '');
  filterRoleSelect('editGRoleMultSelect', '');

  document.getElementById('editGId').value = g.id;
  document.getElementById('editGTitle').value = g.title || '';
  document.getElementById('editGDesc').value = g.description || '';
  document.getElementById('editGBanner').value = g.banner_url || '';
  document.getElementById('editGNetwork').value = g.network || 'Ethereum';
  // Set select values after channels/roles are loaded
  const mentionRoleSel = document.getElementById('editGMentionRole');
  if (mentionRoleSel) {
    // Try setting value; if option not found yet, store for after load
    mentionRoleSel.value = g.mention_role || '';
  }
  const editChSel = document.getElementById('editGChannel');
  if (editChSel) {
    editChSel.value = g.channel_id || '';
  }
  if (document.getElementById('editGWinnerChannel')) {
    document.getElementById('editGWinnerChannel').value = g.winner_channel_id || '';
  }

  document.getElementById('editGMinPerUser').value = g.min_per_user || 1;
  document.getElementById('editGMaxPerUser').value = g.max_per_user || 1;
  document.getElementById('editGDurationVal').value = g.duration_val || 15;
  document.getElementById('editGDurationUnit').value = g.duration_unit || 'hours';

  const previewBox = document.getElementById('editGBannerPreview');
  if (previewBox) {
    if (g.banner_url) {
      previewBox.style.display = 'block';
      previewBox.querySelector('img').src = g.banner_url;
    } else {
      previewBox.style.display = 'none';
    }
  }

  // Populate spot tiers
  const tierContainer = document.getElementById('editSpotTiersList');
  tierContainer.innerHTML = '';
  editSpotTierCount = 0;
  if (g.spot_tiers && g.spot_tiers.length) {
    g.spot_tiers.forEach(t => addEditSpotTier(t.name, t.count));
  } else {
    addEditSpotTier('Guaranteed', g.guaranteed_spots || 3);
    addEditSpotTier('FCFS', g.fcfs_spots || 20);
  }

  // Populate dynamic tasks
  const taskContainer = document.getElementById('editDynamicTasksList');
  taskContainer.innerHTML = '';
  editDynamicTaskCount = 0;
  if (g.tasks?.dynamic_tasks && g.tasks.dynamic_tasks.length) {
    g.tasks.dynamic_tasks.forEach(t => addEditDynamicTask(t.type, t.value));
  } else if (g.tasks) {
    if (g.tasks.twitter_follow) addEditDynamicTask('twitter_follow', g.tasks.twitter_follow);
    if (g.tasks.twitter_like) addEditDynamicTask('twitter_like', g.tasks.twitter_like);
    if (g.tasks.twitter_retweet) addEditDynamicTask('twitter_retweet', g.tasks.twitter_retweet);
    if (g.tasks.twitter_comment) addEditDynamicTask('twitter_comment', g.tasks.twitter_comment);
    if (g.tasks.discord_join) addEditDynamicTask('discord_join', g.tasks.discord_join);
    if (g.tasks.discord_server) addEditDynamicTask('discord_server', g.tasks.discord_server);
    if (g.tasks.tiktok_follow) addEditDynamicTask('tiktok_follow', g.tasks.tiktok_follow);
    if (g.tasks.youtube_follow) addEditDynamicTask('youtube_follow', g.tasks.youtube_follow);
    if (g.tasks.manual_task) addEditDynamicTask('manual_task', g.tasks.manual_task);
  }

  // Populate required roles badge chips
  const rawRoles = (g.tasks && g.tasks.roles) ? (Array.isArray(g.tasks.roles) ? g.tasks.roles : [g.tasks.roles]) : [];
  editRequiredRoles = rawRoles.map(rid => {
    const strId = String(rid).trim();
    const found = (cachedServerRoles || []).find(r => String(r.id) === strId || r.name.toLowerCase() === strId.toLowerCase());
    return {
      id: strId,
      name: found ? found.name : strId
    };
  });
  renderEditRequiredRoles();

  // Populate role multipliers
  editRoleMultipliers = [];
  if (g.role_multipliers && Array.isArray(g.role_multipliers)) {
    editRoleMultipliers = g.role_multipliers.map(rm => ({
      id: String(rm.id || '').trim(),
      name: rm.name || rm.id || '',
      multiplier: parseInt(rm.multiplier || rm.entries || 1) || 1
    }));
  }
  renderEditRoleMultipliers();

  document.getElementById('editReqEvm').checked = !!g.tasks?.require_evm;
  document.getElementById('editReqSolana').checked = !!g.tasks?.require_solana;

  // Social Links
  if (document.getElementById('editGTwitterLink')) document.getElementById('editGTwitterLink').value = g.social_links?.twitter_link || '';
  if (document.getElementById('editGDiscordLink')) document.getElementById('editGDiscordLink').value = g.social_links?.discord_link || '';
  if (document.getElementById('editGTelegramLink')) document.getElementById('editGTelegramLink').value = g.social_links?.telegram_link || '';
  if (document.getElementById('editGWebsiteLink')) document.getElementById('editGWebsiteLink').value = g.social_links?.website_link || '';

  openModal('editModal');
}

let isSubmittingEdit = false;
async function submitEditGiveaway() {
  if (isSubmittingEdit) return;

  const btn = document.getElementById('editSaveBtn');
  if (btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.innerHTML = '⏳ Saving...';
  }
  isSubmittingEdit = true;

  try {
    const gId = document.getElementById('editGId').value;
    const g = currentGiveaways.find(x => x.id === gId);
    if (!g) return;

    const title = document.getElementById('editGTitle').value.trim();
    const description = document.getElementById('editGDesc').value.trim();
    const banner_url = document.getElementById('editGBanner').value.trim();
    const network = document.getElementById('editGNetwork').value.trim() || 'Ethereum';
    const mention_role = document.getElementById('editGMentionRole').value;
    const channelSelect = document.getElementById('editGChannel') ? document.getElementById('editGChannel').value : '';
    const channel_id = channelSelect || g.channel_id || '';

    const winnerChannelSelect = document.getElementById('editGWinnerChannel') ? document.getElementById('editGWinnerChannel').value : '';
    const winnerChannelManual = document.getElementById('editGWinnerChannelManual') ? document.getElementById('editGWinnerChannelManual').value.trim() : '';
    const winner_channel_id = winnerChannelManual || winnerChannelSelect || '';

    const min_per_user = parseInt(document.getElementById('editGMinPerUser').value) || 1;
    const max_per_user = parseInt(document.getElementById('editGMaxPerUser').value) || 1;
    const duration_val = parseFloat(document.getElementById('editGDurationVal').value) || 15;
    const duration_unit = document.getElementById('editGDurationUnit').value;

    const spot_tiers = getEditSpotTiersPayload();
    const dynamic_tasks = getEditDynamicTasksPayload();
    const require_evm = document.getElementById('editReqEvm').checked;
    const require_solana = document.getElementById('editReqSolana').checked;

    const twitter_link = document.getElementById('editGTwitterLink')?.value.trim() || '';
    const discord_link = document.getElementById('editGDiscordLink')?.value.trim() || '';
    const telegram_link = document.getElementById('editGTelegramLink')?.value.trim() || '';
    const website_link = document.getElementById('editGWebsiteLink')?.value.trim() || '';
    const social_links = { twitter_link, discord_link, telegram_link, website_link };

    let durationInSeconds = duration_val * 60;
    if (duration_unit === 'hours') durationInSeconds = duration_val * 3600;
    if (duration_unit === 'days') durationInSeconds = duration_val * 86400;

    g.title = title;
    g.description = description;
    g.banner_url = banner_url;
    g.network = network;
    g.mention_role = mention_role;
    g.winner_channel_id = winner_channel_id;
    g.channel_id = channel_id;
    g.social_links = social_links;

    const editSelectedRoles = editRequiredRoles.map(r => r.id);

    g.min_per_user = min_per_user;
    g.max_per_user = max_per_user;
    g.duration_val = duration_val;
    g.duration_unit = duration_unit;
    g.ends_at = g.created_at + durationInSeconds;
    g.spot_tiers = spot_tiers;
    g.role_multipliers = editRoleMultipliers;
    g.tasks = {
      dynamic_tasks,
      require_evm,
      require_solana,
      roles: editSelectedRoles
    };

    try {
      const res = await fetch(apiUrl(`/api/giveaways/${gId}/edit`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(g)
      });
      await firebasePut(`giveaways/${gId}`, g);
      showToast('Giveaway updated successfully', 'success');
    } catch (err) {
      await firebasePut(`giveaways/${gId}`, g);
      showToast('Giveaway updated', 'success');
    }

    closeModal('editModal');
    closeModal('detailModal');
    await loadGiveaways();
  } finally {
    isSubmittingEdit = false;
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = 'Save Changes';
    }
  }
}

async function deleteGiveaway(giveawayId) {
  if (!confirm('Are you sure you want to delete this giveaway? This will permanently delete it from the database, website, and Discord.')) return;

  try {
    await fetch(apiUrl(`/api/giveaways/${giveawayId}/delete`), {
      method: 'POST',
      credentials: 'include'
    });
  } catch (err) {
    console.warn('Delete API call error:', err);
  }

  try {
    await firebasePut(`giveaways/${giveawayId}`, null);
    await firebasePut(`giveaway_entries/${giveawayId}`, null);
  } catch (fe) {
    console.warn('Firebase client delete error:', fe);
  }

  // Immediately remove from currentGiveaways in local memory
  currentGiveaways = currentGiveaways.filter(x => x.id !== giveawayId);
  renderGiveaways();
  showToast('Giveaway deleted', 'success');

  closeModal('detailModal');
  closeModal('editModal');
  await loadGiveaways();
}

// Helper: Format Winners Text for Beautiful Web Display
function formatWinnersForWeb(winnersText) {
  if (!winnersText || typeof winnersText !== 'string' || !winnersText.trim()) return '';
  const lines = winnersText.split('\n').map(l => l.trim()).filter(Boolean);
  if (lines.length === 0) return '';

  return lines.map(line => {
    if (line.includes(':')) {
      const parts = line.split(':');
      const category = parts[0].replace(/[*_`]/g, '').trim();
      const mentions = parts.slice(1).join(':').trim();

      let icon = svgTrophy();
      let catColor = '#fbbf24';
      let borderLeft = '#eab308';
      let bgStyle = 'rgba(234, 179, 8, 0.12)';

      const lowerCat = category.toLowerCase();
      if (lowerCat.includes('guaranteed') || lowerCat.includes('gtd')) {
        icon = svgTrophy();
        catColor = '#fbbf24';
        borderLeft = '#eab308';
        bgStyle = 'rgba(234, 179, 8, 0.12)';
      } else if (lowerCat.includes('fcfs')) {
        icon = svgTrophy();
        catColor = '#c084fc';
        borderLeft = '#a855f7';
        bgStyle = 'rgba(168, 85, 247, 0.12)';
      } else if (lowerCat.includes('tier 1')) {
        icon = svgTrophy();
        catColor = '#fbbf24';
        borderLeft = '#eab308';
      } else if (lowerCat.includes('tier 2')) {
        icon = svgTrophy();
        catColor = '#94a3b8';
        borderLeft = '#64748b';
      } else if (lowerCat.includes('tier 3')) {
        icon = svgTrophy();
        catColor = '#fb923c';
        borderLeft = '#f97316';
      }

      return `
        <div style="background: ${bgStyle}; padding: 10px 14px; border-radius: var(--radius-sm); border-left: 4px solid ${borderLeft}; margin-bottom: 6px;">
          <div style="font-weight: 700; color: ${catColor}; font-size: 0.92rem; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
            <span>${icon}</span> <span>${escapeHtml(category)}</span>
          </div>
          <div style="color: #f8fafc; font-size: 0.9rem; line-height: 1.6; word-break: break-word;">
            ${escapeHtml(mentions)}
          </div>
        </div>
      `;
    }
    return `<div style="padding: 6px 12px; background: rgba(0,0,0,0.25); border-radius: var(--radius-sm); color: #f8fafc; font-size: 0.9rem; margin-bottom: 4px;">${escapeHtml(line)}</div>`;
  }).join('');
}

// Global cache for public entries to support instant live search
let allPublicEntries = [];
let currentPublicWalletField = 'evm_wallet';
let currentDetailWinnersText = '';

// Open Detail & Admin Verification Modal
async function openDetailModal(giveawayId) {
  let g = currentGiveaways.find(x => x.id === giveawayId);
  if (!g) {
    // Fetch directly from API or Firebase if not in current memory
    try {
      const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        g = data.giveaway || data;
      }
      if (!g) {
        g = await firebaseGet('giveaways/' + giveawayId);
      }
      if (g && g.id) {
        currentGiveaways.push(g);
      }
    } catch (fetchErr) {
      console.warn('Direct fetch giveaway detail failed:', fetchErr);
    }
  }

  if (!g) {
    showToast('Giveaway not found or removed', 'error');
    return;
  }

  const isAdmin = currentUser && currentUser.is_admin;
  activeDetailGiveaway = g;
  currentDetailWinnersText = g.winners_text || '';
  document.getElementById('detailTitle').innerText = g.title;
  
  const content = document.getElementById('detailContent');
  const now = Math.floor(Date.now() / 1000);
  const isEnded = !g.is_active || g.ends_at <= now;

  // 1. Build Spot Tiers / Prizes Box (Tessera Badges)
  let spotTiersHtml = '';
  if (g.spot_tiers && g.spot_tiers.length > 0) {
    const tiersBadges = g.spot_tiers.map(t => `<span class="tier-badge tier-badge-generic font-mono">${escapeHtml(t.name || 'Tier')}: <b>${t.count || 1} spots</b></span>`).join(' ');
    spotTiersHtml = `
      <div class="tessera-tiers-wrap">
        <div class="tessera-tiers-label">Spot Allocation / Prize Tiers</div>
        <div class="tessera-tiers-badges">${tiersBadges}</div>
      </div>
    `;
  } else if (g.guaranteed_spots || g.fcfs_spots) {
    const gtd = g.guaranteed_spots || 0;
    const fcfs = g.fcfs_spots || 0;
    spotTiersHtml = `
      <div class="tessera-tiers-wrap">
        <div class="tessera-tiers-label">Spot Allocation</div>
        <div class="tessera-tiers-badges">
          <span class="tier-badge tier-badge-gtd font-mono">Guaranteed: <b>${gtd} spots</b></span>
          <span class="tier-badge tier-badge-fcfs font-mono">FCFS: <b>${fcfs} spots</b></span>
        </div>
      </div>
    `;
  }

  // 2. Build task requirements list for public view
  const reqs = [];
  if (g.tasks?.twitter_follow) reqs.push(`<li>${svgTwitter()} Follow <b>@${escapeHtml(g.tasks.twitter_follow)}</b> on X</li>`);
  if (g.tasks?.twitter_like) reqs.push(`<li>${svgCheck()} Like specified Tweet</li>`);
  if (g.tasks?.twitter_retweet) reqs.push(`<li>${svgCheck()} Retweet specified Tweet</li>`);
  if (g.tasks?.tiktok_follow) reqs.push(`<li>${svgCheck()} Follow TikTok</li>`);
  if (g.tasks?.youtube_follow) reqs.push(`<li>${svgCheck()} Subscribe YouTube</li>`);
  if (g.tasks?.roles?.length) reqs.push(`<li>${svgShield()} Required Roles: ${escapeHtml(g.tasks.roles.join(', '))}</li>`);
  if (g.tasks?.manual_task) reqs.push(`<li>${svgCheck()} ${escapeHtml(g.tasks.manual_task)}</li>`);
  if (g.tasks?.dynamic_tasks) {
    g.tasks.dynamic_tasks.forEach(t => {
      reqs.push(`<li>${svgCheck()} ${escapeHtml(t.value)}</li>`);
    });
  }

    let totalWinnersCount = 0;
  if (g.spot_tiers && g.spot_tiers.length) {
    totalWinnersCount = g.spot_tiers.reduce((acc, t) => acc + (parseInt(t.count) || 0), 0);
  } else {
    totalWinnersCount = (g.guaranteed_spots || 0) + (g.fcfs_spots || 0);
  }
  if (!totalWinnersCount) totalWinnersCount = 1;

  content.innerHTML = `
    <div style="display: flex; flex-direction: column; gap: 1rem;">
      <div class="tessera-detail-hero">
        ${g.banner_url ? `
          <div class="tessera-banner-wrap">
            <img src="${escapeHtml(g.banner_url)}" class="tessera-banner-img" alt="banner" onerror="this.parentElement.style.display='none'">
            <div class="tessera-banner-overlay"></div>
          </div>
        ` : ''}
        
        <div class="tessera-header-content">
          <div class="tessera-project-row">
            <div class="tessera-chip">
              <span class="tessera-chip-label">Hosted by</span>
              <span class="tessera-chip-val">${escapeHtml(g.hosted_by || 'Admin')}</span>
            </div>
            <div class="tessera-chip">
              <span class="tessera-chip-label">Network</span>
              <span class="tessera-chip-val">${escapeHtml(g.network || 'Ethereum')}</span>
            </div>
            <div class="tessera-chip">
              <span class="tessera-chip-label">Raffle ID</span>
              <span class="tessera-chip-val font-mono" style="color: var(--text-muted);">${escapeHtml(g.id)}</span>
            </div>
          </div>

          <div style="font-size: 0.88rem; color: var(--text-secondary); line-height: 1.6; margin-bottom: 0.75rem;">
            ${formatMarkdownDescription(g.description)}
          </div>
          ${renderSocialButtonsHTML(g.social_links)}
        </div>

        <!-- 4-Cell Metric Grid (Tessera) -->
        <div class="tessera-metric-grid">
          <div class="tessera-grid-cell">
            <span class="tessera-grid-label">${svgUsers()} Entries</span>
            <span class="tessera-grid-val tabular-nums">${g.entries_count || 0}</span>
          </div>
          <div class="tessera-grid-cell">
            <span class="tessera-grid-label">${svgTrophy()} Winners</span>
            <span class="tessera-grid-val tabular-nums">${totalWinnersCount}×</span>
          </div>
          <div class="tessera-grid-cell">
            <span class="tessera-grid-label">${svgShield()} Requirements</span>
            <span class="tessera-grid-val tabular-nums">${reqs.length}</span>
          </div>
          <div class="tessera-grid-cell">
            <span class="tessera-grid-label">${svgClock()} Closes in</span>
            <span class="tessera-grid-val font-mono" style="font-size: 1rem;">${isEnded ? 'Closed' : getTimeLeftString(g.ends_at)}</span>
          </div>
        </div>

        ${spotTiersHtml}
      </div>

      ${(g.role_multipliers && g.role_multipliers.length) ? `
        <div style="background: rgba(245, 158, 11, 0.05); border: 1px solid rgba(245, 158, 11, 0.2); border-radius: var(--radius-sm); padding: 0.85rem 1rem;">
          <div style="font-family: var(--font-mono); font-size: 0.68rem; color: #fbbf24; text-transform: uppercase; letter-spacing: 0.16em; font-weight: 700; margin-bottom: 6px;">Role Entry Multipliers</div>
          <div style="display: flex; flex-wrap: wrap; gap: 6px;">
            ${g.role_multipliers.map(rm => `<span class="tier-badge tier-badge-gtd font-mono">@${escapeHtml(rm.name || rm.id)}: <b>${rm.multiplier}x Tickets</b></span>`).join(' ')}
          </div>
        </div>
      ` : ''}

      <div id="userBonusEntriesSection"></div>

      <div class="tessera-reqs-card">
        <div class="tessera-reqs-title">
          ${svgShield()}
          <span>Task Requirements</span>
        </div>
        <ul class="tessera-reqs-list">
          ${reqs.length ? reqs.join('') : '<li style="color: var(--text-muted);">No extra requirements specified. Open to all members.</li>'}
        </ul>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
        <button class="btn btn-outline btn-sm" onclick="copyShareLink('${g.id}')">
          ${svgShare()}
          <span>Share Raffle Link</span>
        </button>
        ${!currentUser ? '<span style="font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-muted);">Sign in with Discord to view or submit profile</span>' : ''}
      </div>
    </div>
  `;

  // Public participants list (visible to everyone)
  await loadPublicParticipants(giveawayId, g.network || 'Ethereum', g.winners_text || '');

  // Populate logged-in user bonus entries widget
  const bonusContainer = document.getElementById('userBonusEntriesSection');
  if (bonusContainer) {
    if (currentUser && currentUser.id) {
      const myEntry = (allPublicEntries || []).find(e => String(e.user_id) === String(currentUser.id));
      const availBonus = currentUser.bonus_entries || 0;
      if (myEntry && !isEnded) {
        bonusContainer.innerHTML = `
          <div style="background: rgba(234, 179, 8, 0.08); border: 1px solid rgba(234, 179, 8, 0.25); border-radius: var(--radius-sm); padding: 0.85rem 1rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
            <div>
              <div style="font-weight: 700; color: #fbbf24; font-size: 0.9rem;">Your Entries: ${myEntry.multiplier || 1}x ${myEntry.bonus_entries_used ? `(+${myEntry.bonus_entries_used} Bonus = ${(myEntry.multiplier || 1) + myEntry.bonus_entries_used}x Total)` : ''}</div>
              <div style="font-size: 0.8rem; color: var(--text-muted);">Available Bonus Entries in Profile: <b>${availBonus}</b></div>
            </div>
            ${availBonus > 0 ? `
              <div style="display: flex; gap: 6px; align-items: center;">
                <input type="number" id="detailBonusAmount" class="form-input" min="1" max="${availBonus}" value="1" style="width: 70px; padding: 4px 8px; font-size: 0.85rem;">
                <button class="btn btn-primary btn-sm" onclick="submitApplyBonusEntries('${g.id}')">Apply Bonus Entries</button>
              </div>
            ` : ''}
          </div>
        `;
      } else {
        bonusContainer.innerHTML = '';
      }
    } else {
      bonusContainer.innerHTML = '';
    }
  }

  // Admin Box setup
  const adminBox = document.getElementById('adminControlBox');
  if (isAdmin) {
    adminBox.style.display = 'block';
    await loadGiveawayParticipants(giveawayId);
    
    document.getElementById('editGiveawayAdminBtn').onclick = () => openEditModal(giveawayId);
    document.getElementById('deleteGiveawayAdminBtn').onclick = () => deleteGiveaway(giveawayId);
    document.getElementById('drawWinnersBtn').onclick = () => drawWinners(giveawayId);
    document.getElementById('redrawWinnersBtn').onclick = () => redrawWinners(giveawayId);
    if (document.getElementById('announceWinnersBtn')) {
      document.getElementById('announceWinnersBtn').onclick = () => sendWinnersAnnouncement(giveawayId);
    }
    document.getElementById('exportAllEntriesBtn').onclick = () => exportAllEntriesCSV(giveawayId);
    document.getElementById('exportWinnersBtn').onclick = () => exportWinnersCSV(giveawayId);

    // Setup Mark Done button & Lock notice banner
    const markDoneBtn = document.getElementById('markDoneAdminBtn');
    const lockNotice = document.getElementById('lockStatusNotice');
    if (markDoneBtn) {
      if (g.is_done) {
        markDoneBtn.innerHTML = 'Locked — Click to Re-open';
        markDoneBtn.className = 'btn btn-outline-warning btn-sm';
        if (lockNotice) {
          lockNotice.style.display = 'block';
          lockNotice.style.background = 'rgba(234, 179, 8, 0.12)';
          lockNotice.style.border = '1px solid rgba(234, 179, 8, 0.4)';
          lockNotice.style.color = '#facc15';
          lockNotice.innerHTML = '<strong>Sheet Locked &amp; Frozen:</strong> This giveaway is marked as <strong>DONE</strong>. Participant and winner EVM / FCFS EVM wallet addresses are permanently frozen for distribution and will not change when users update their profiles.';
        }
      } else {
        markDoneBtn.innerHTML = 'Lock Sheet';
        markDoneBtn.className = 'btn btn-success btn-sm';
        if (lockNotice) {
          lockNotice.style.display = 'block';
          lockNotice.style.background = 'rgba(16, 185, 129, 0.1)';
          lockNotice.style.border = '1px solid rgba(16, 185, 129, 0.3)';
          lockNotice.style.color = '#34d399';
          lockNotice.innerHTML = '<strong>Live Sync Active:</strong> Participant &amp; winner EVM / FCFS EVM addresses automatically update live if users edit their profiles. Click <strong>"Done (Lock Sheet)"</strong> when ready to freeze addresses for distribution.';
        }
      }
      markDoneBtn.onclick = () => toggleGiveawayDone(giveawayId);
    }
  } else {
    adminBox.style.display = 'none';
  }

  openModal('detailModal');
}

async function toggleGiveawayDone(giveawayId) {
  const g = allGiveaways.find(x => x.id === giveawayId);
  const currentlyDone = g && g.is_done;
  const actionText = currentlyDone 
    ? "Unlock this giveaway sheet? Live syncing of user profile wallets will be re-enabled."
    : "Mark this giveaway as DONE? This will permanently FREEZE all participant and winner wallet addresses (Main EVM and FCFS EVM) so user profile edits will no longer alter the distribution sheet or CSV downloads.";

  if (!confirm(actionText)) return;

  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/mark-done`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ is_done: !currentlyDone })
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      showToast(data.error || 'Failed to update giveaway done status', 'error');
      return;
    }

    showToast(data.message || (data.is_done ? 'Giveaway marked Done. Sheet is frozen.' : 'Giveaway unlocked.'), 'success');
    
    // Update local giveaway object
    if (g) {
      g.is_done = data.is_done;
      if (data.giveaway) Object.assign(g, data.giveaway);
    }
    
    // Refresh modal and giveaways list
    await fetchGiveaways();
    openDetailModal(giveawayId);
  } catch (err) {
    console.error('Toggle done error:', err);
    showToast('Failed to update status', 'error');
  }
}

function copyShareLink(giveawayId) {
  const shareUrl = `${window.location.origin}/?giveaway=${giveawayId}`;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(shareUrl).then(() => {
      showToast('Share link copied to clipboard', 'success');
    }).catch(() => {
      prompt('Copy share link:', shareUrl);
    });
  } else {
    prompt('Copy share link:', shareUrl);
  }
}

// Determine which wallet field to show based on network name
function getWalletFieldForNetwork(network) {
  const n = (network || '').toLowerCase().trim();
  if (n === 'solana' || n === 'sol') return { field: 'solana_wallet', label: 'Solana Wallet' };
  // All EVM-compatible chains
  return { field: 'evm_wallet', label: 'Wallet Address' };
}

// Load Public Participants (visible to everyone)
async function loadPublicParticipants(giveawayId, network, winnersText = '') {
  const tbody = document.getElementById('publicParticipantsBody');
  const countBadge = document.getElementById('publicParticipantCountBadge');
  const searchInput = document.getElementById('publicParticipantSearch');
  const walletHeader = document.getElementById('publicWalletHeader');
  if (searchInput) searchInput.value = '';
  if (!tbody) return;

  const walletInfo = getWalletFieldForNetwork(network);
  currentPublicWalletField = walletInfo.field;
  if (walletHeader) walletHeader.innerText = walletInfo.label;

  tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color: var(--text-muted); padding: 1rem;">Loading participants...</td></tr>';

  try {
    let entries = [];

    // 1. Fetch from backend API first (which applies live profile wallet sync if not Done, or frozen if Done)
    try {
      const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        entries = data.entries || [];
      }
    } catch (e) {
      console.warn('Backend API detail fetch failed, falling back to Firebase:', e);
    }

    // 2. Fallback to Firebase directly
    if (!entries || entries.length === 0) {
      const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
      if (fbData && typeof fbData === 'object') {
        entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
      }
    }

    allPublicEntries = (entries || []).filter(Boolean);
    if (countBadge) countBadge.innerText = allPublicEntries.length;

    renderPublicParticipantsTable(allPublicEntries, currentPublicWalletField, winnersText);
  } catch (err) {
    console.error('Error loading public participants:', err);
    tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #ff4757;">Failed to load participants.</td></tr>';
  }
}

function renderPublicParticipantsTable(entries, walletField, winnersText = '') {
  const tbody = document.getElementById('publicParticipantsBody');
  if (!tbody) return;

  if (!entries || entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 2rem;">No participants found.</td></tr>';
    return;
  }

  tbody.innerHTML = entries.map((e, idx) => {
    if (!e) return '';
    const wallet = e[walletField] || e.evm_wallet || e.solana_wallet || 'Not provided';
    const uid = String(e.user_id || '');
    const uname = e.username || e.display_name || 'User';
    const rankNum = String(idx + 1).padStart(2, '0');
    const avatarUrl = getDiscordAvatar(uid, e.avatar, uname);

    // Check winner status from winner_type or winnersText
    let statusBadge = '<span class="font-mono text-muted" style="font-size: 0.72rem; text-transform: uppercase;">Participant</span>';
    const wType = String(e.winner_type || '').toLowerCase();
    const isWinnerMentioned = winnersText && (winnersText.includes(uid) || (uname && winnersText.toLowerCase().includes(uname.toLowerCase())));

    if (wType.includes('gtd') || wType.includes('guaranteed') || (winnersText.includes('Guaranteed') && isWinnerMentioned)) {
      statusBadge = '<span class="tier-badge tier-badge-gtd font-mono">Guaranteed</span>';
    } else if (wType.includes('fcfs') || (winnersText.includes('FCFS') && isWinnerMentioned)) {
      statusBadge = '<span class="tier-badge tier-badge-fcfs font-mono">FCFS</span>';
    } else if (wType || isWinnerMentioned) {
      statusBadge = '<span class="tier-badge tier-badge-gtd font-mono">Winner</span>';
    }

    const mult = e.multiplier || 1;
    const bonusUsed = e.bonus_entries_used || 0;
    const ticketText = `${mult}x${bonusUsed ? ` (+${bonusUsed})` : ''}`;

    const walletPill = (wallet && wallet !== 'Not provided')
      ? `<span class="wallet-copy-pill" onclick="copyText('${escapeHtml(wallet)}', 'Wallet address')" title="Click to copy">${truncateAddress(wallet)} ${svgCopy()}</span>`
      : `<span style="color: var(--text-faint); font-family: var(--font-mono); font-size: 0.72rem;">None</span>`;

    return `
      <tr>
        <td><span class="p-rank-num">#${rankNum}</span></td>
        <td>
          <div class="participant-user-cell">
            <img src="${escapeHtml(avatarUrl)}" class="p-avatar" alt="" onerror="this.onerror=null; this.src='https://cdn.discordapp.com/embed/avatars/0.png';">
            <div class="p-user-names">
              <span class="p-display-name">${escapeHtml(e.display_name || uname)}</span>
              <span class="p-user-handle">@${escapeHtml(uname)}</span>
            </div>
          </div>
        </td>
        <td><code class="font-mono" style="font-size: 0.75rem; color: var(--text-muted);">${escapeHtml(uid || 'N/A')}</code></td>
        <td><span class="tier-badge tier-badge-generic font-mono">${ticketText}</span></td>
        <td>${walletPill}</td>
        <td>${statusBadge}</td>
      </tr>
    `;
  }).join('');
}

function filterPublicParticipants(query) {
  const q = (query || '').toLowerCase().trim();
  if (!q) {
    renderPublicParticipantsTable(allPublicEntries, currentPublicWalletField, currentDetailWinnersText);
    return;
  }
  const filtered = allPublicEntries.filter(e => {
    if (!e) return false;
    const uname = String(e.username || e.display_name || '').toLowerCase();
    const uid = String(e.user_id || '').toLowerCase();
    const wallet = String(e[currentPublicWalletField] || e.evm_wallet || e.solana_wallet || '').toLowerCase();
    const wType = String(e.winner_type || '').toLowerCase();
    return uname.includes(q) || uid.includes(q) || wallet.includes(q) || wType.includes(q);
  });
  renderPublicParticipantsTable(filtered, currentPublicWalletField, currentDetailWinnersText);
}

// Deep Linking: Direct Giveaway URL detector
async function checkUrlDirectGiveaway() {
  const urlParams = new URLSearchParams(window.location.search);
  let gId = urlParams.get('giveaway') || urlParams.get('id') || urlParams.get('g');

  if (!gId) {
    const pathMatch = window.location.pathname.match(/^\/(?:giveaway|g)\/([^\/]+)/i);
    if (pathMatch) {
      gId = pathMatch[1];
    }
  }

  if (gId) {
    gId = gId.trim();
    await openDirectGiveawayView(gId);
  }
}

async function openDirectGiveawayView(giveawayId) {
  try {
    let g = currentGiveaways.find(x => x.id === giveawayId);
    if (!g) {
      // Fetch directly from API or Firebase
      const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        g = data.giveaway || data;
      }
      if (!g) {
        g = await firebaseGet('giveaways/' + giveawayId);
      }
      if (g && g.id) {
        currentGiveaways.push(g);
      }
    }

    if (g) {
      // Show Direct Giveaway Banner on Main Page
      const banner = document.getElementById('singleGiveawayBanner');
      const bannerTitle = document.getElementById('singleGiveawayBannerTitle');
      const openBtn = document.getElementById('singleGiveawayOpenBtn');
      if (banner && bannerTitle) {
        bannerTitle.innerText = g.title || 'Giveaway';
        banner.style.display = 'flex';
        if (openBtn) {
          openBtn.onclick = () => openDetailModal(g.id);
        }
      }

      // Render grid highlighting this giveaway
      renderGiveaways(g);

      // Automatically open the full detail modal with participants & winners!
      await openDetailModal(g.id);
    } else {
      showToast('Giveaway not found or has been removed.', 'error');
    }
  } catch (err) {
    console.error('Direct giveaway view error:', err);
  }
}

function showAllGiveawaysView() {
  const banner = document.getElementById('singleGiveawayBanner');
  if (banner) banner.style.display = 'none';

  const lbSection = document.getElementById('leaderboardSection');
  if (lbSection) lbSection.style.display = 'none';

  const feedHeader = document.querySelector('.feed-controls-header');
  if (feedHeader) feedHeader.style.display = 'flex';

  const grid = document.getElementById('giveawayGrid');
  if (grid) grid.style.display = 'grid';

  const navRaffles = document.getElementById('navRafflesBtn');
  if (navRaffles) navRaffles.classList.add('active');
  const navLb = document.getElementById('navLeaderboardBtn');
  if (navLb) navLb.classList.remove('active');
  const tabLb = document.getElementById('tabLeaderboardBtn');
  if (tabLb) tabLb.classList.remove('active');
  const tabAct = document.getElementById('tabActiveBtn');
  if (tabAct) tabAct.classList.add('active');

  // Clean URL query without page reload
  if (window.history && window.history.pushState) {
    window.history.pushState({}, document.title, window.location.pathname);
  }
  renderGiveaways();
}

async function showLeaderboardView() {
  const banner = document.getElementById('singleGiveawayBanner');
  if (banner) banner.style.display = 'none';

  const grid = document.getElementById('giveawayGrid');
  if (grid) grid.style.display = 'none';

  const feedHeader = document.querySelector('.feed-controls-header');
  if (feedHeader) feedHeader.style.display = 'none';

  const lbSection = document.getElementById('leaderboardSection');
  if (lbSection) lbSection.style.display = 'block';

  const navRaffles = document.getElementById('navRafflesBtn');
  if (navRaffles) navRaffles.classList.remove('active');
  const navLb = document.getElementById('navLeaderboardBtn');
  if (navLb) navLb.classList.add('active');
  const tabLb = document.getElementById('tabLeaderboardBtn');
  if (tabLb) tabLb.classList.add('active');
  const tabAct = document.getElementById('tabActiveBtn');
  if (tabAct) tabAct.classList.remove('active');

  lbSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  await loadBonusLeaderboard();
}

function focusEntryTracker() {
  const lbSection = document.getElementById('leaderboardSection');
  if (lbSection && lbSection.style.display !== 'none') {
    showAllGiveawaysView();
  }
  const tracker = document.getElementById('tracker');
  if (tracker) {
    tracker.scrollIntoView({ behavior: 'smooth', block: 'center' });
    const input = document.getElementById('globalEntrySearchInput');
    if (input) {
      input.focus();
      input.select();
    }
  }
}

function filterByMyEntries() {
  if (!currentUser || !currentUser.id) {
    showToast('Please sign in to view your entries', 'info');
    return;
  }
  showAllGiveawaysView();
  const input = document.getElementById('globalEntrySearchInput');
  if (input) {
    input.value = currentUser.username || currentUser.id;
    handleGlobalEntrySearch(input.value);
  }
  focusEntryTracker();
}

// Load Participants into Admin Table with Winner Highlighting
async function loadGiveawayParticipants(giveawayId) {
  const tbody = document.getElementById('participantsTableBody');
  tbody.innerHTML = '<tr><td colspan="8">Loading entries...</td></tr>';
  try {
    let entries = [];
    
    // 1. Fetch from backend API first (which applies live profile wallet sync if not Done, or frozen if Done)
    try {
      const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        entries = data.entries || [];
        if (data.giveaway) {
          const idx = allGiveaways.findIndex(x => x.id === giveawayId);
          if (idx !== -1) allGiveaways[idx] = data.giveaway;
        }
      }
    } catch (e) {
      console.warn('Backend API detail fetch failed, trying Firebase:', e);
    }

    // 2. Fallback to Firebase Cloud DB
    if (!entries || entries.length === 0) {
      const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
      if (fbData && typeof fbData === 'object') {
        entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
      }
    }
    
    if (!entries || entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No entries recorded yet. Users click [Join Giveaway] on Discord or website to participate!</td></tr>';
      return;
    }

    tbody.innerHTML = entries.map(e => {
      if (!e) return '';
      const isWinner = !!e.winner_type;
      const winnerBadge = isWinner 
        ? `<span class="g-badge ${String(e.winner_type).toLowerCase().includes('guarantee') ? 'g-badge-guaranteed' : 'g-badge-fcfs'}" style="font-weight: bold; padding: 3px 8px;">WINNER (${escapeHtml(String(e.winner_type).toUpperCase())})</span>`
        : '<span style="color: var(--text-muted);">Participant</span>';
      
      const nameStyle = isWinner ? 'color: #ffd700; font-weight: bold;' : 'font-weight: bold;';
      const mult = e.multiplier || 1;
      const bonusUsed = e.bonus_entries_used || 0;
      const ticketBadge = `<span class="badge" style="background: rgba(234, 179, 8, 0.15); color: #fde047; border: 1px solid rgba(234, 179, 8, 0.3); font-weight: 700; padding: 2px 8px; border-radius: 4px;">${mult}x Tickets${bonusUsed ? ` (+${bonusUsed})` : ''}</span>`;

      return `
        <tr style="${isWinner ? 'background: rgba(255, 215, 0, 0.08);' : ''}">
          <td>
            <div class="participant-user-cell">
              <img src="${escapeHtml(getDiscordAvatar(e.user_id, e.avatar, e.username))}" class="p-avatar" alt="" onerror="this.onerror=null; this.src='https://cdn.discordapp.com/embed/avatars/0.png';">
              <div class="p-user-names">
                <span class="p-display-name" style="${nameStyle}">${escapeHtml(e.display_name || e.username || 'User')}</span>
                <span class="p-user-handle">@${escapeHtml(e.username || 'user')} · ID: ${escapeHtml(e.user_id || 'N/A')}</span>
              </div>
            </div>
          </td>
          <td>${winnerBadge}</td>
          <td>${ticketBadge}</td>
          <td><code>${escapeHtml(e.evm_wallet || 'None')}</code></td>
          <td><code>${escapeHtml(e.fcfs_evm_wallet || e.burner_evm_wallet || 'None')}</code></td>
          <td><code>${escapeHtml(e.solana_wallet || 'None')}</code></td>
          <td>
            <span style="font-size: 0.8rem;">
              Twitter: ${escapeHtml(e.twitter || '-')}<br>
              Telegram: ${escapeHtml(e.telegram || '-')}
            </span>
          </td>
          <td>
            <div style="display: flex; gap: 6px; align-items: center;">
              <select onchange="updateVerificationStatus('${giveawayId}', '${e.user_id}', this.value)" class="form-select" style="padding: 4px 8px; font-size: 0.8rem;">
                <option value="verified" ${e.task_status === 'verified' || !e.task_status ? 'selected' : ''}>Verified</option>
                <option value="pending" ${e.task_status === 'pending' ? 'selected' : ''}>Pending</option>
                <option value="ineligible" ${e.task_status === 'ineligible' ? 'selected' : ''}>Ineligible</option>
              </select>
              <button type="button" class="btn btn-danger btn-sm" style="padding: 3px 7px; font-size: 0.8rem;" onclick="deleteParticipantEntry('${giveawayId}', '${e.user_id}')" title="Delete Entry">${svgTrash()}</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    console.error('Error loading participants:', err);
    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #ff4757;">Failed to load entries. Please try refreshing.</td></tr>';
  }
}

// Admin Winner Drawing
let isDrawingWinners = false;
async function drawWinners(giveawayId) {
  if (isDrawingWinners) return;
  if (!confirm('Are you sure you want to draw/assign winners for this giveaway?')) return;
  isDrawingWinners = true;
  const btn = document.getElementById('drawWinnersBtn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/draw`), { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) {
      showToast('Winners selected & announced to Discord', 'success');
      await loadGiveawayParticipants(giveawayId);
      await loadGiveaways();
    } else {
      showToast(data.error || 'Failed to draw winners', 'error');
    }
  } catch (err) {
    showToast('Error drawing winners', 'error');
  } finally {
    isDrawingWinners = false;
    if (btn) btn.disabled = false;
  }
}

// Admin Winner Re-Drawing (Re-Raffle Disqualified Spots)
let isRedrawingWinners = false;
async function redrawWinners(giveawayId) {
  if (isRedrawingWinners) return;
  if (!confirm('Are you sure you want to re-raffle replacement winners for any disqualified spots?')) return;
  isRedrawingWinners = true;
  const btn = document.getElementById('redrawWinnersBtn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/redraw`), { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) {
      showToast('Replacement winners re-raffled & announced to Discord', 'success');
      await loadGiveawayParticipants(giveawayId);
      await loadGiveaways();
    } else {
      showToast(data.error || 'Failed to re-raffle winners', 'error');
    }
  } catch (err) {
    showToast('Error re-raffling winners', 'error');
  } finally {
    isRedrawingWinners = false;
    if (btn) btn.disabled = false;
  }
}

// Admin: Delete single participant entry
async function deleteParticipantEntry(giveawayId, userId) {
  if (!confirm('Are you sure you want to remove this participant entry?')) return;
  try {
    try {
      await fetch(apiUrl(`/api/giveaways/${giveawayId}/entries/${userId}/delete`), { method: 'POST', credentials: 'include' });
    } catch (e) {}

    // Direct Firebase REST delete sync
    const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
    if (fbData && typeof fbData === 'object') {
      const entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
      const filtered = entries.filter(e => e && String(e.user_id) !== String(userId));
      await firebasePut('giveaway_entries/' + giveawayId, filtered);
    }
    showToast('Participant entry removed', 'success');
    await loadGiveawayParticipants(giveawayId);
    await loadGiveaways();
  } catch (err) {
    showToast('Error deleting entry', 'error');
  }
}

// Admin: Send Winners Announcement manually
let isSendingAnnouncement = false;
async function sendWinnersAnnouncement(giveawayId) {
  if (isSendingAnnouncement) return;
  isSendingAnnouncement = true;
  const btn = document.getElementById('announceWinnersBtn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/announce`), { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) {
      showToast('Winners Announcement posted to Discord', 'success');
    } else {
      showToast(data.error || 'Failed to post announcement', 'error');
    }
  } catch (err) {
    showToast('Error sending announcement', 'error');
  } finally {
    isSendingAnnouncement = false;
    if (btn) btn.disabled = false;
  }
}

// Update Verification Status
async function updateVerificationStatus(giveawayId, userId, status) {
  try {
    // 1. Send update to Backend API
    let updated = false;
    try {
      const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/verify-winner`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ user_id: userId, task_status: status })
      });
      if (res.ok) updated = true;
    } catch (e) {
      console.warn('Backend API verify call failed, using direct Firebase update:', e);
    }

    // 2. Direct Firebase sync for guaranteed client resilience
    const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
    if (fbData && typeof fbData === 'object') {
      const entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
      const target = entries.find(e => e && String(e.user_id) === String(userId));
      if (target) {
        target.task_status = status;
        if (status === 'ineligible') target.winner_type = null;
        await firebasePut('giveaway_entries/' + giveawayId, entries);
        updated = true;
      }
    }

    if (updated) {
      showToast(`Updated status to ${status}`, 'success');
    } else {
      showToast('Status saved', 'info');
    }
  } catch (err) {
    showToast('Error updating status', 'error');
  }
}

// Export All Entries as CSV (Live synced before Done, frozen after Done)
async function exportAllEntriesCSV(giveawayId) {
  try {
    let entries = [];

    // 1. Fetch from backend API first (which applies live sync if not marked Done, or frozen if Done)
    try {
      const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        entries = data.entries || [];
      }
    } catch (apiErr) {
      console.warn('Backend API detail fetch failed, falling back to Firebase:', apiErr);
    }

    // 2. Fallback to Firebase
    if (!entries || entries.length === 0) {
      const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
      if (fbData && typeof fbData === 'object') {
        entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
      }
    }

    if (!entries || entries.length === 0) {
      showToast('No entries recorded yet to download.', 'info');
      return;
    }

    let csv = '\uFEFFDiscord Username,Discord ID,Twitter Handle,Telegram Handle,Main EVM Wallet,FCFS EVM Wallet,Solana Wallet,Entries (Tickets),Bonus Entries Used,Task Status,Winner Status\n';
    entries.forEach(e => {
      if (!e) return;
      const winnerStatus = e.winner_type ? `WINNER (${String(e.winner_type).toUpperCase()})` : 'Participant';
      const tickets = `${e.multiplier || 1}x`;
      const bonusUsed = e.bonus_entries_used || 0;
      const fcfsWallet = e.fcfs_evm_wallet || e.burner_evm_wallet || '';
      csv += `"${(e.username || e.display_name || 'User').replace(/"/g, '""')}","${e.user_id || ''}","${(e.twitter || '').replace(/"/g, '""')}","${(e.telegram || '').replace(/"/g, '""')}","${e.evm_wallet || ''}","${fcfsWallet}","${e.solana_wallet || ''}","${tickets}","${bonusUsed}","${e.task_status || 'verified'}","${winnerStatus}"\n`;
    });

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.style.display = 'none';
    a.href = url;
    a.download = `giveaway_${giveawayId}_all_entries.csv`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    }, 200);

    showToast('Exported all entries to CSV', 'success');
  } catch (err) {
    console.error('CSV export error:', err);
    showToast('Failed to export entries', 'error');
  }
}

// Export Winners as CSV (Live synced before Done, frozen after Done)
async function exportWinnersCSV(giveawayId) {
  try {
    let entries = [];

    // 1. Fetch from backend API first
    try {
      const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        entries = data.entries || [];
      }
    } catch (apiErr) {
      console.warn('Backend API detail fetch failed, falling back to Firebase:', apiErr);
    }

    // 2. Fallback to Firebase
    if (!entries || entries.length === 0) {
      const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
      if (fbData && typeof fbData === 'object') {
        entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
      }
    }

    const winners = entries.filter(e => e && e.winner_type);
    if (winners.length === 0) {
      showToast('No winners to export yet.', 'info');
      return;
    }

    let csv = '\uFEFFDiscord Username,Discord ID,Spot Type,Main EVM Wallet,FCFS EVM Wallet,Solana Wallet,Entries (Tickets),Twitter Handle,Telegram Handle,Task Status\n';
    winners.forEach(w => {
      const fcfsWallet = w.fcfs_evm_wallet || w.burner_evm_wallet || '';
      csv += `"${(w.username || w.display_name || 'User').replace(/"/g, '""')}","${w.user_id || ''}","${String(w.winner_type).toUpperCase()}","${w.evm_wallet || ''}","${fcfsWallet}","${w.solana_wallet || ''}","${w.multiplier || 1}x","${(w.twitter || '').replace(/"/g, '""')}","${(w.telegram || '').replace(/"/g, '""')}","${w.task_status || 'verified'}"\n`;
    });

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.style.display = 'none';
    a.href = url;
    a.download = `giveaway_${giveawayId}_winners.csv`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    }, 200);

    showToast('Exported winners to CSV', 'success');
  } catch (err) {
    console.error('Winners CSV export error:', err);
    showToast('Failed to export winners', 'error');
  }
}

// User Profile Modal Setup & Save
function openProfileModal() {
  if (!currentUser) return;
  document.getElementById('profTwitter').value = currentUser.twitter || '';
  document.getElementById('profTelegram').value = currentUser.telegram || '';
  document.getElementById('profEvm').value = currentUser.evm_wallet || '';
  const burnerInp = document.getElementById('profBurnerEvm');
  if (burnerInp) burnerInp.value = currentUser.fcfs_evm_wallet || currentUser.burner_evm_wallet || '';
  document.getElementById('profSolana').value = currentUser.solana_wallet || '';
  openModal('profileModal');
}

async function submitSaveProfile() {
  const twitter = document.getElementById('profTwitter').value.trim();
  const telegram = document.getElementById('profTelegram').value.trim();
  const evm_wallet = document.getElementById('profEvm').value.trim();
  const fcfs_evm_wallet = document.getElementById('profBurnerEvm') ? document.getElementById('profBurnerEvm').value.trim() : '';
  const solana_wallet = document.getElementById('profSolana').value.trim();

  const evmRegex = /^0x[a-fA-F0-9]{40}$/;
  if (!evm_wallet || !evmRegex.test(evm_wallet)) {
    showToast('Main EVM Wallet is mandatory (valid 0x address).', 'error');
    return;
  }
  if (!fcfs_evm_wallet || !evmRegex.test(fcfs_evm_wallet)) {
    showToast('FCFS EVM Wallet is mandatory (valid 0x address).', 'error');
    return;
  }

  try {
    const res = await fetch(apiUrl('/api/user/profile'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        twitter,
        telegram,
        evm_wallet,
        fcfs_evm_wallet,
        burner_evm_wallet: fcfs_evm_wallet,
        solana_wallet
      })
    });
    if (res.ok) {
      showToast('Profile and wallets updated!', 'success');
      closeModal('profileModal');
      await checkAuth();
    } else {
      const errData = await res.json().catch(() => ({}));
      showToast(errData.error || 'Failed to update profile', 'error');
    }
  } catch (err) {
    showToast('Error saving profile', 'error');
  }
}

async function submitApplyBonusEntries(giveawayId) {
  const input = document.getElementById('detailBonusAmount');
  const amount = parseInt(input ? input.value : 1) || 1;
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/apply-bonus-entries`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ amount })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Applied ${amount} bonus entries! Total tickets: ${data.total_tickets}x`, 'success');
      if (currentUser) currentUser.bonus_entries = data.remaining_bonus_entries;
      await openDetailModal(giveawayId);
    } else {
      showToast(data.error || 'Failed to apply bonus entries', 'error');
    }
  } catch (err) {
    showToast('Error applying bonus entries', 'error');
  }
}

// Live Member Search & Custom Winner Selection State
let selectedGtdWinners = [];
let selectedFcfsWinners = [];
let memberSearchDebounceTimer = null;

function openCustomWinnersModal() {
  selectedGtdWinners = [];
  selectedFcfsWinners = [];
  document.getElementById('memberSearchInput').value = '';
  document.getElementById('memberSearchResults').style.display = 'none';
  document.getElementById('gtdWinnersManual').value = '';
  document.getElementById('fcfsWinnersManual').value = '';
  renderSelectedWinnersTags();
  openModal('customWinnersModal');
}

function onMemberSearchInput(query) {
  clearTimeout(memberSearchDebounceTimer);
  const container = document.getElementById('memberSearchResults');
  if (!query.trim()) {
    container.style.display = 'none';
    return;
  }

  memberSearchDebounceTimer = setTimeout(async () => {
    try {
      const res = await fetch(apiUrl(`/api/members/search?q=${encodeURIComponent(query.trim())}`), { credentials: 'include' });
      if (!res.ok) return;
      const members = await res.json();
      
      if (!members || members.length === 0) {
        container.innerHTML = `<div style="padding: 12px; color: var(--text-muted); font-size: 0.85rem; text-align: center;">No matching members found</div>`;
      } else {
        container.innerHTML = members.map(m => `
          <div class="member-search-item">
            <img src="${escapeHtml(m.avatar)}" alt="${escapeHtml(m.display_name)}">
            <div class="member-search-info">
              <span class="member-search-name">${escapeHtml(m.display_name)} (@${escapeHtml(m.username)})</span>
              <span class="member-search-sub">ID: ${escapeHtml(m.id)} ${m.evm_wallet ? '| EVM: ' + escapeHtml(m.evm_wallet.substring(0,6)) + '...' : ''}</span>
            </div>
            <div class="member-search-actions">
              <button type="button" class="btn btn-primary btn-sm" onclick="addSelectedWinner('gtd', '${escapeHtml(m.id)}', '${escapeHtml(m.display_name)}', '${escapeHtml(m.username)}', '${escapeHtml(m.avatar)}')">+ GTD</button>
              <button type="button" class="btn btn-purple btn-sm" onclick="addSelectedWinner('fcfs', '${escapeHtml(m.id)}', '${escapeHtml(m.display_name)}', '${escapeHtml(m.username)}', '${escapeHtml(m.avatar)}')">+ FCFS</button>
            </div>
          </div>
        `).join('');
      }
      container.style.display = 'block';
    } catch (err) {
      console.error('Member search error:', err);
    }
  }, 200);
}

function addSelectedWinner(type, id, displayName, username, avatar) {
  const item = { id, displayName, username, avatar, mention: `<@${id}>` };
  if (type === 'gtd') {
    if (!selectedGtdWinners.some(w => w.id === id)) selectedGtdWinners.push(item);
  } else {
    if (!selectedFcfsWinners.some(w => w.id === id)) selectedFcfsWinners.push(item);
  }
  document.getElementById('memberSearchResults').style.display = 'none';
  document.getElementById('memberSearchInput').value = '';
  renderSelectedWinnersTags();
}

function removeSelectedWinner(type, id) {
  if (type === 'gtd') {
    selectedGtdWinners = selectedGtdWinners.filter(w => w.id !== id);
  } else {
    selectedFcfsWinners = selectedFcfsWinners.filter(w => w.id !== id);
  }
  renderSelectedWinnersTags();
}

function renderSelectedWinnersTags() {
  const gtdBox = document.getElementById('gtdWinnersTags');
  const fcfsBox = document.getElementById('fcfsWinnersTags');
  
  document.getElementById('gtdSelectedCount').innerText = `${selectedGtdWinners.length} Selected`;
  document.getElementById('fcfsSelectedCount').innerText = `${selectedFcfsWinners.length} Selected`;

  if (selectedGtdWinners.length === 0) {
    gtdBox.innerHTML = `<span class="placeholder-text" style="color: var(--text-muted); font-size: 0.82rem;">Selected GTD winners will appear here...</span>`;
  } else {
    gtdBox.innerHTML = selectedGtdWinners.map(w => `
      <span class="winner-pill-tag">
        <img src="${escapeHtml(w.avatar)}" alt="">
        <span>${escapeHtml(w.displayName)} (@${escapeHtml(w.username)})</span>
        <span class="winner-pill-remove" onclick="removeSelectedWinner('gtd', '${escapeHtml(w.id)}')">&times;</span>
      </span>
    `).join('');
  }

  if (selectedFcfsWinners.length === 0) {
    fcfsBox.innerHTML = `<span class="placeholder-text" style="color: var(--text-muted); font-size: 0.82rem;">Selected FCFS winners will appear here...</span>`;
  } else {
    fcfsBox.innerHTML = selectedFcfsWinners.map(w => `
      <span class="winner-pill-tag">
        <img src="${escapeHtml(w.avatar)}" alt="">
        <span>${escapeHtml(w.displayName)} (@${escapeHtml(w.username)})</span>
        <span class="winner-pill-remove" onclick="removeSelectedWinner('fcfs', '${escapeHtml(w.id)}')">&times;</span>
      </span>
    `).join('');
  }
}

async function submitCustomWinners() {
  if (!currentDetailId) return;

  const manualGtd = document.getElementById('gtdWinnersManual').value.trim();
  const manualFcfs = document.getElementById('fcfsWinnersManual').value.trim();

  const gtdMentions = [...selectedGtdWinners.map(w => w.mention), manualGtd].filter(Boolean).join(', ');
  const fcfsMentions = [...selectedFcfsWinners.map(w => w.mention), manualFcfs].filter(Boolean).join(', ');

  if (!gtdMentions && !fcfsMentions) {
    showToast('Please select or enter at least one winner for GTD or FCFS', 'warning');
    return;
  }

  try {
    const res = await fetch(apiUrl(`/api/giveaways/${currentDetailId}/set-custom-winners`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        guaranteed_winners: gtdMentions,
        fcfs_winners: fcfsMentions
      })
    });

    if (res.ok) {
      showToast('Custom winners set & announced to Discord', 'success');
      closeModal('customWinnersModal');
      closeModal('detailModal');
      await loadGiveaways();
    } else {
      const data = await res.json();
      showToast(data.error || 'Failed to set custom winners', 'error');
    }
  } catch (err) {
    console.error('Custom winners submit error:', err);
    showToast('Error setting custom winners', 'error');
  }
}

// Utility Modal Helpers
function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.add('active');
    const searchInputs = modal.querySelectorAll('.select-search-input');
    searchInputs.forEach(input => {
      input.value = '';
    });
    if (id === 'createModal') {
      filterChannelSelect('gChannel', '');
      filterChannelSelect('gWinnerChannel', '');
      filterRoleSelect('gMentionRole', '');
      filterRoleSelect('gReqRoleSelect', '');
      filterRoleSelect('gRoleMultSelect', '');
    } else if (id === 'editModal') {
      filterChannelSelect('editGChannel', '');
      filterChannelSelect('editGWinnerChannel', '');
      filterRoleSelect('editGMentionRole', '');
      filterRoleSelect('editGReqRoleSelect', '');
      filterRoleSelect('editGRoleMultSelect', '');
    }
  }
}
function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.remove('active');
}

function showToast(msg, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<span>${escapeHtml(msg)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 4000);
}

function getTimeLeftString(timestamp) {
  const diff = timestamp - Math.floor(Date.now() / 1000);
  if (diff <= 0) return 'Ended';
  const hours = Math.floor(diff / 3600);
  const mins = Math.floor((diff % 3600) / 60);
  if (hours > 24) return `${Math.floor(hours / 24)} days left`;
  return `${hours}h ${mins}m left`;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, function (m) {
    return {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    }[m];
  });
}

// ==========================================================================
// BONUS ENTRIES LEADERBOARD & ENTRY TRACKER ENGINE
// ==========================================================================
let cachedLeaderboardData = [];
let _globalSearchDebounce = null;

async function loadBonusLeaderboard(forceRefresh = false) {
  const tbody = document.getElementById('leaderboardTableBody');
  if (tbody && (!cachedLeaderboardData.length || forceRefresh)) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align: center; padding: 2.5rem; color: var(--text-muted); font-family: var(--font-mono); font-size: 0.85rem;">
          Loading bonus entries leaderboard...
        </td>
      </tr>
    `;
  }

  try {
    let list = [];
    let totalSum = 0;
    let topHolder = 'None';

    // 1. Attempt to fetch from Backend API first
    try {
      const res = await fetch(apiUrl('/api/bonus-leaderboard'), { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        if (data && data.success && Array.isArray(data.leaderboard)) {
          list = data.leaderboard;
          totalSum = data.total_bonus_sum || 0;
          topHolder = data.top_holder || 'None';
        }
      }
    } catch (apiErr) {
      console.warn('Backend leaderboard API unreachable, using Cloud DB fallback:', apiErr);
    }

    // 2. Fallback to Firebase Cloud DB aggregation
    if (!list.length) {
      let profiles = {};
      let entriesByGid = {};
      try {
        profiles = (await firebaseGet('user_profiles')) || {};
      } catch (e) {
        console.warn('Firebase user_profiles fetch failed:', e);
      }

      try {
        entriesByGid = (await firebaseGet('giveaway_entries')) || {};
      } catch (e) {
        console.warn('Firebase giveaway_entries fetch failed:', e);
      }

      const usedByUser = {};
      const gwCountByUser = {};

      if (entriesByGid && typeof entriesByGid === 'object') {
        Object.values(entriesByGid).forEach(gEntries => {
          const arr = Array.isArray(gEntries) ? gEntries : (gEntries ? Object.values(gEntries) : []);
          arr.forEach(e => {
            if (!e || !e.user_id) return;
            const uid = String(e.user_id);
            const bUsed = parseInt(e.bonus_entries_used, 10) || 0;
            usedByUser[uid] = (usedByUser[uid] || 0) + bUsed;
            gwCountByUser[uid] = (gwCountByUser[uid] || 0) + 1;
          });
        });
      }

      const allUids = new Set([...Object.keys(profiles), ...Object.keys(usedByUser)]);
      allUids.forEach(uid => {
        const prof = profiles[uid] || {};
        const availBonus = parseInt(prof.bonus_entries, 10) || 0;
        const usedBonus = usedByUser[uid] || 0;
        const totalBonus = availBonus + usedBonus;
        const gwCount = gwCountByUser[uid] || 0;

        if (totalBonus <= 0 && gwCount <= 0) return;

        const uName = (prof.username || `user_${uid.slice(-4)}`).trim();
        const dName = (prof.display_name || uName).trim();

        list.push({
          user_id: uid,
          username: uName,
          display_name: dName,
          avatar: prof.avatar || '',
          available_bonus: availBonus,
          used_bonus: usedBonus,
          total_bonus: totalBonus,
          giveaways_entered: gwCount,
          evm_wallet: prof.evm_wallet || '',
          solana_wallet: prof.solana_wallet || '',
          fcfs_wallet: prof.fcfs_evm_wallet || prof.burner_evm_wallet || '',
          twitter: prof.twitter || ''
        });
      });

      list.sort((a, b) => {
        if (b.total_bonus !== a.total_bonus) return b.total_bonus - a.total_bonus;
        if (b.available_bonus !== a.available_bonus) return b.available_bonus - a.available_bonus;
        return b.giveaways_entered - a.giveaways_entered;
      });

      list.forEach((item, idx) => {
        item.rank = idx + 1;
      });

      totalSum = list.reduce((sum, item) => sum + item.total_bonus, 0);
      topHolder = list.length > 0 ? (list[0].display_name || list[0].username) : 'None';
    }

    cachedLeaderboardData = list;

    // Update 4-Cell Metric Bar
    const totalBonusValEl = document.getElementById('lbTotalBonusVal');
    const totalUsersValEl = document.getElementById('lbTotalUsersVal');
    const topHolderValEl = document.getElementById('lbTopHolderVal');
    const activeRafflesValEl = document.getElementById('lbActiveRafflesVal');

    if (totalBonusValEl) totalBonusValEl.textContent = totalSum.toLocaleString();
    if (totalUsersValEl) totalUsersValEl.textContent = list.length.toLocaleString();
    if (topHolderValEl) topHolderValEl.textContent = topHolder;
    if (activeRafflesValEl) {
      const activeCount = (currentGiveaways || []).filter(g => g.is_active && (!g.ends_at || g.ends_at > Math.floor(Date.now() / 1000))).length;
      activeRafflesValEl.textContent = activeCount.toString();
    }

    renderBonusLeaderboard(cachedLeaderboardData);

    // If user has typed in table filter or personal lookup, apply filter
    const searchInput = document.getElementById('lbUserSearchInput');
    const tableFilter = document.getElementById('lbTableFilterInput');
    const query = (searchInput?.value || tableFilter?.value || '').trim();
    if (query) {
      filterBonusLeaderboard(query);
    }
  } catch (err) {
    console.error('Failed to load bonus leaderboard:', err);
    if (tbody) {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" style="text-align: center; padding: 2.5rem; color: #f87171; font-family: var(--font-mono); font-size: 0.85rem;">
            Failed to load leaderboard data. Please check connection and click Refresh.
          </td>
        </tr>
      `;
    }
  }
}

function renderBonusLeaderboard(list) {
  const tbody = document.getElementById('leaderboardTableBody');
  const countBadge = document.getElementById('lbShowingCountBadge');
  if (countBadge) countBadge.textContent = `Showing ${list.length} users`;
  if (!tbody) return;

  if (!list || list.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align: center; padding: 2.5rem; color: var(--text-muted); font-family: var(--font-mono); font-size: 0.85rem;">
          No ranked participants found.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = list.map(item => {
    const rank = item.rank;
    let rankClass = '';
    let medalSvg = '';
    if (rank === 1) {
      rankClass = 'top-1';
      medalSvg = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:2px; vertical-align:middle;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>`;
    } else if (rank === 2) {
      rankClass = 'top-2';
    } else if (rank === 3) {
      rankClass = 'top-3';
    }
    const rankFormatted = rank < 10 ? `#0${rank}` : `#${rank}`;
    const avatarUrl = getDiscordAvatar(item.user_id, item.avatar, item.username);

    // Formatted Wallet Pills
    const evm = item.evm_wallet ? `
      <div style="font-family: var(--font-mono); font-size: 0.74rem; color: var(--text-secondary); display: inline-flex; align-items: center; gap: 4px; cursor: pointer; background: rgba(59, 130, 246, 0.08); padding: 2px 6px; border-radius: var(--radius-xs); border: 1px solid rgba(59, 130, 246, 0.2);" onclick="copyToClipboard('${item.evm_wallet}', this)" title="Click to copy EVM">
        <span style="color: #60a5fa; font-weight: 700;">EVM:</span> ${item.evm_wallet.slice(0, 6)}...${item.evm_wallet.slice(-4)}
      </div>
    ` : '';

    const sol = item.solana_wallet ? `
      <div style="font-family: var(--font-mono); font-size: 0.74rem; color: var(--text-secondary); display: inline-flex; align-items: center; gap: 4px; cursor: pointer; background: rgba(139, 92, 246, 0.08); padding: 2px 6px; border-radius: var(--radius-xs); border: 1px solid rgba(139, 92, 246, 0.2);" onclick="copyToClipboard('${item.solana_wallet}', this)" title="Click to copy Solana">
        <span style="color: #a78bfa; font-weight: 700;">SOL:</span> ${item.solana_wallet.slice(0, 5)}...${item.solana_wallet.slice(-4)}
      </div>
    ` : '';

    const fcfs = item.fcfs_wallet ? `
      <div style="font-family: var(--font-mono); font-size: 0.74rem; color: var(--text-secondary); display: inline-flex; align-items: center; gap: 4px; cursor: pointer; background: rgba(16, 185, 129, 0.08); padding: 2px 6px; border-radius: var(--radius-xs); border: 1px solid rgba(16, 185, 129, 0.2);" onclick="copyToClipboard('${item.fcfs_wallet}', this)" title="Click to copy FCFS">
        <span style="color: #34d399; font-weight: 700;">FCFS:</span> ${item.fcfs_wallet.slice(0, 6)}...${item.fcfs_wallet.slice(-4)}
      </div>
    ` : '';

    const walletsHtml = (evm || sol || fcfs) ? `<div class="wallet-badge-cell">${evm}${sol}${fcfs}</div>` : `<span style="color: var(--text-faint); font-family: var(--font-mono); font-size: 0.75rem;">None registered</span>`;

    return `
      <tr>
        <td>
          <span class="rank-badge ${rankClass}">
            ${medalSvg}${rankFormatted}
          </span>
        </td>
        <td>
          <div class="lb-user-cell">
            <img src="${avatarUrl}" class="lb-user-avatar" alt="${escapeHtml(item.username)}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
            <div class="lb-user-info">
              <span class="lb-user-name">${escapeHtml(item.display_name || item.username)}</span>
              <span class="lb-user-sub">@${escapeHtml(item.username)} &bull; ${escapeHtml(item.user_id)}</span>
            </div>
          </div>
        </td>
        <td style="text-align: center;">
          <span style="font-family: var(--font-mono); font-weight: 700; color: #34d399; font-size: 0.95rem;">
            ${item.available_bonus || 0}
          </span>
        </td>
        <td style="text-align: center;">
          <span style="font-family: var(--font-mono); font-weight: 600; color: var(--text-secondary); font-size: 0.9rem;">
            +${item.used_bonus || 0}
          </span>
        </td>
        <td style="text-align: center;">
          <span class="bonus-val-pill">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polygon points="12 6 15 11 20 11 16 14 18 19 12 16 6 19 8 14 4 11 9 11 12 6"></polygon></svg>
            ${item.total_bonus || 0}
          </span>
        </td>
        <td>
          ${walletsHtml}
        </td>
      </tr>
    `;
  }).join('');
}

function filterBonusLeaderboard(query) {
  const q = (query || '').trim().toLowerCase();
  const personalCard = document.getElementById('lbPersonalStatsCard');

  // Sync inputs
  const userSearch = document.getElementById('lbUserSearchInput');
  const tableFilter = document.getElementById('lbTableFilterInput');
  if (userSearch && userSearch.value !== query) userSearch.value = query;
  if (tableFilter && tableFilter.value !== query) tableFilter.value = query;

  if (!q) {
    if (personalCard) personalCard.style.display = 'none';
    renderBonusLeaderboard(cachedLeaderboardData);
    return;
  }

  const filtered = cachedLeaderboardData.filter(item => {
    const uName = (item.username || '').toLowerCase();
    const dName = (item.display_name || '').toLowerCase();
    const uid = (item.user_id || '').toLowerCase();
    const evm = (item.evm_wallet || '').toLowerCase();
    const sol = (item.solana_wallet || '').toLowerCase();
    return uName.includes(q) || dName.includes(q) || uid.includes(q) || evm.includes(q) || sol.includes(q);
  });

  // Spotlight card for the closest matched participant
  if (filtered.length > 0 && personalCard) {
    const topMatch = filtered[0];
    const avatarUrl = getDiscordAvatar(topMatch.user_id, topMatch.avatar, topMatch.username);
    personalCard.style.display = 'block';
    personalCard.innerHTML = `
      <div class="tracker-results-user-header">
        <div class="tracker-user-profile">
          <img src="${avatarUrl}" class="tracker-avatar" alt="${escapeHtml(topMatch.username)}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
          <div class="tracker-names">
            <span class="tracker-display-name">${escapeHtml(topMatch.display_name || topMatch.username)}</span>
            <span class="tracker-username">@${escapeHtml(topMatch.username)} &bull; Discord ID: ${escapeHtml(topMatch.user_id)}</span>
          </div>
        </div>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <span class="rank-badge ${topMatch.rank <= 3 ? 'top-' + topMatch.rank : ''}" style="width: auto; padding: 4px 10px;">
            Rank #${topMatch.rank}
          </span>
          <span class="tracker-bonus-badge">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polygon points="12 6 15 11 20 11 16 14 18 19 12 16 6 19 8 14 4 11 9 11 12 6"></polygon></svg>
            ${topMatch.total_bonus} Total Bonus Entries
          </span>
        </div>
      </div>
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-top: 10px;">
        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-subtle); padding: 8px 12px; border-radius: var(--radius-xs);">
          <div style="font-family: var(--font-mono); font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase;">Available Balance</div>
          <div style="font-family: var(--font-mono); font-size: 1.15rem; font-weight: 700; color: #34d399;">${topMatch.available_bonus || 0}</div>
        </div>
        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-subtle); padding: 8px 12px; border-radius: var(--radius-xs);">
          <div style="font-family: var(--font-mono); font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase;">Used in Raffles</div>
          <div style="font-family: var(--font-mono); font-size: 1.15rem; font-weight: 700; color: var(--text-pure);">+${topMatch.used_bonus || 0}</div>
        </div>
        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-subtle); padding: 8px 12px; border-radius: var(--radius-xs);">
          <div style="font-family: var(--font-mono); font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase;">Raffles Entered</div>
          <div style="font-family: var(--font-mono); font-size: 1.15rem; font-weight: 700; color: #60a5fa;">${topMatch.giveaways_entered || 0}</div>
        </div>
      </div>
    `;
  } else if (personalCard) {
    personalCard.style.display = 'none';
  }

  renderBonusLeaderboard(filtered);
}

// Live Global Entry Tracker (Hero Search Bar)
async function handleGlobalEntrySearch(query) {
  clearTimeout(_globalSearchDebounce);
  _globalSearchDebounce = setTimeout(async () => {
    const q = (query || '').trim().toLowerCase();
    const panel = document.getElementById('trackerResultsPanel');
    if (!panel) return;

    if (!q) {
      panel.style.display = 'none';
      panel.innerHTML = '';
      return;
    }

    panel.style.display = 'block';
    panel.innerHTML = `
      <div style="text-align: center; padding: 1.5rem; color: var(--text-muted); font-family: var(--font-mono); font-size: 0.85rem;">
        Searching active & past giveaways for "${escapeHtml(q)}"...
      </div>
    `;

    let matchedProfile = null;
    try {
      const profiles = (await firebaseGet('user_profiles')) || {};
      for (const [uid, prof] of Object.entries(profiles)) {
        if (!prof) continue;
        const u = (prof.username || '').toLowerCase();
        const d = (prof.display_name || '').toLowerCase();
        const evm = (prof.evm_wallet || '').toLowerCase();
        const sol = (prof.solana_wallet || '').toLowerCase();
        if (uid === q || u === q || d === q || u.includes(q) || d.includes(q) || evm === q || sol === q) {
          matchedProfile = { uid, ...prof };
          break;
        }
      }
    } catch (e) {
      console.warn('Profile search fallback error:', e);
    }

    const enteredRaffles = [];
    const targetUid = matchedProfile ? matchedProfile.uid : q;

    for (const g of (currentGiveaways || [])) {
      try {
        let entries = [];
        const fbEntries = await firebaseGet(`giveaway_entries/${g.id}`);
        if (fbEntries) {
          entries = Array.isArray(fbEntries) ? fbEntries : Object.values(fbEntries);
        }
        const userEntry = entries.find(e => {
          if (!e) return false;
          const eUid = String(e.user_id || '').toLowerCase();
          const eUser = String(e.username || '').toLowerCase();
          const eDisp = String(e.display_name || '').toLowerCase();
          const eEvm = String(e.evm_wallet || '').toLowerCase();
          return eUid === targetUid.toLowerCase() || eUser === q || eDisp === q || eUser.includes(q) || eEvm === q;
        });

        if (userEntry) {
          enteredRaffles.push({ giveaway: g, entry: userEntry });
        }
      } catch (err) {}
    }

    const displayName = (matchedProfile && (matchedProfile.display_name || matchedProfile.username)) || (enteredRaffles[0] && (enteredRaffles[0].entry.display_name || enteredRaffles[0].entry.username)) || q;
    const username = (matchedProfile && matchedProfile.username) || (enteredRaffles[0] && enteredRaffles[0].entry.username) || q;
    const uid = (matchedProfile && matchedProfile.uid) || (enteredRaffles[0] && enteredRaffles[0].entry.user_id) || q;
    const avatarUrl = getDiscordAvatar(uid, matchedProfile?.avatar, username);
    const availBonus = (matchedProfile && matchedProfile.bonus_entries) || 0;

    let totalBonusUsed = 0;
    enteredRaffles.forEach(item => {
      totalBonusUsed += (item.entry.bonus_entries_used || 0);
    });

    panel.innerHTML = `
      <div class="tracker-results-user-header">
        <div class="tracker-user-profile">
          <img src="${avatarUrl}" class="tracker-avatar" alt="${escapeHtml(username)}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
          <div class="tracker-names">
            <span class="tracker-display-name">${escapeHtml(displayName)}</span>
            <span class="tracker-username">@${escapeHtml(username)} &bull; Discord ID: ${escapeHtml(uid)}</span>
          </div>
        </div>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <span class="tracker-bonus-badge">
            ${availBonus} Available Bonus
          </span>
          <button class="btn btn-outline btn-sm" onclick="showLeaderboardView(); filterBonusLeaderboard('${escapeHtml(username)}');">
            View on Leaderboard
          </button>
        </div>
      </div>

      <div style="margin-top: 1rem;">
        <div style="font-family: var(--font-mono); font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.14em; margin-bottom: 0.75rem;">
          Verified Raffle Entries (${enteredRaffles.length})
        </div>
        ${enteredRaffles.length === 0 ? `
          <div style="padding: 1rem; text-align: center; color: var(--text-muted); font-family: var(--font-mono); font-size: 0.82rem; background: rgba(255,255,255,0.01); border: 1px dashed var(--border-subtle); border-radius: var(--radius-xs);">
            No raffle entries found for this user in current giveaways.
          </div>
        ` : `
          <div style="display: flex; flex-direction: column; gap: 8px;">
            ${enteredRaffles.map(({ giveaway: g, entry: e }) => {
              const multiplier = e.multiplier || 1;
              const bonusUsed = e.bonus_entries_used || 0;
              const totalTickets = multiplier + bonusUsed;
              const wallet = e.evm_wallet || e.solana_wallet || 'No wallet registered';
              const isEnded = !g.is_active || (g.ends_at && g.ends_at <= Math.floor(Date.now() / 1000));
              return `
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; padding: 0.75rem 1rem; background: rgba(255,255,255,0.02); border: 1px solid var(--border-subtle); border-radius: var(--radius-xs);">
                  <div>
                    <div style="font-weight: 700; color: var(--text-pure); font-size: 0.92rem; display: flex; align-items: center; gap: 8px;">
                      <span>${escapeHtml(g.title || 'Raffle')}</span>
                      <span class="status-pill ${isEnded ? 'ended' : 'live'}" style="font-size: 0.65rem; padding: 1px 6px;">
                        ${isEnded ? 'Ended' : 'Active'}
                      </span>
                    </div>
                    <div style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); margin-top: 2px;">
                      Wallet: <span style="color: var(--text-secondary); cursor: pointer;" onclick="copyToClipboard('${wallet}', this)">${wallet.length > 20 ? wallet.slice(0, 8) + '...' + wallet.slice(-6) : wallet}</span>
                    </div>
                  </div>
                  <div style="display: flex; align-items: center; gap: 10px;">
                    <span style="font-family: var(--font-mono); font-size: 0.85rem; font-weight: 700; color: #fbbf24;">
                      ${totalTickets}x Tickets ${bonusUsed > 0 ? `(+${bonusUsed} Bonus)` : ''}
                    </span>
                    <button class="btn btn-outline btn-sm" style="padding: 3px 8px; font-size: 0.75rem;" onclick="openGiveawayModal('${g.id}')">
                      Details
                    </button>
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        `}
      </div>
    `;
  }, 350);
}
