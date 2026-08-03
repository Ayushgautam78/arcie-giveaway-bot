/* SPA Logic for Arcie Bot Web3 Giveaway Hub */

// Firebase Database URL (HTTPS — works from Vercel)
const FIREBASE_DB = 'https://arcie-bot-default-rtdb.asia-southeast1.firebasedatabase.app';
const ADMIN_PASSWORD = 'innercirclefcfs78@1';

let currentUser = null;
let currentGiveaways = [];
let currentFilter = 'active';
let activeDetailGiveaway = null;

// Helper: Firebase REST read
async function firebaseGet(path) {
  const res = await fetch(`${FIREBASE_DB}/${path}.json`);
  return await res.json();
}

// Helper: Firebase REST write
async function firebasePut(path, data) {
  await fetch(`${FIREBASE_DB}/${path}.json`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initApp();
  setupEventListeners();
});

async function initApp() {
  checkAuth();
  await loadGiveaways();
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
      openModal('createModal');
    });
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
        🔐 Admin Sign In
      </button>
    `;
  }
}

// Admin Password Login
async function submitPasswordLogin(e) {
  e.preventDefault();
  const username = document.getElementById('passUser').value.trim();
  const password = document.getElementById('passWord').value.trim();

  if (password !== ADMIN_PASSWORD) {
    showToast('Invalid password', 'error');
    return;
  }

  currentUser = {
    id: 'admin_' + Date.now(),
    username: username || 'Admin',
    is_admin: true
  };
  localStorage.setItem('arcie_admin', JSON.stringify(currentUser));
  showToast('🚀 Signed in as Admin!', 'success');
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

// Load Giveaways directly from Firebase
async function loadGiveaways() {
  try {
    const data = await firebaseGet('giveaways');
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
  const activeCount = currentGiveaways.filter(g => g.is_active).length;
  let totalSpots = 0;
  let totalEntries = 0;

  currentGiveaways.forEach(g => {
    totalSpots += (g.guaranteed_spots || 0) + (g.fcfs_spots || 0);
    totalEntries += (g.entries_count || 0);
  });

  document.getElementById('statActive').innerText = activeCount;
  document.getElementById('statTotalSpots').innerText = totalSpots;
  document.getElementById('statEntries').innerText = totalEntries;
}

// Render Giveaway Cards
function renderGiveaways() {
  const grid = document.getElementById('giveawayGrid');
  const now = Math.floor(Date.now() / 1000);

  let filtered = currentGiveaways;
  if (currentFilter === 'active') {
    filtered = currentGiveaways.filter(g => g.is_active && g.ends_at > now);
  } else if (currentFilter === 'ended') {
    filtered = currentGiveaways.filter(g => !g.is_active || g.ends_at <= now);
  }

  if (filtered.length === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🎁</div>
        <p>No giveaways found in this category.</p>
      </div>
    `;
    return;
  }

  grid.innerHTML = filtered.map(g => {
    const isEnded = !g.is_active || g.ends_at <= now;
    const timeLeft = getTimeLeftString(g.ends_at);
    
    // Tasks list HTML
    const reqs = [];
    if (g.tasks?.twitter_follow) reqs.push(`<li>🐦 Follow <b>@${escapeHtml(g.tasks.twitter_follow)}</b></li>`);
    if (g.tasks?.twitter_like) reqs.push(`<li>❤️ Like Tweet</li>`);
    if (g.tasks?.twitter_retweet) reqs.push(`<li>🔄 Retweet Tweet</li>`);
    if (g.tasks?.tiktok_follow) reqs.push(`<li>🎵 Follow TikTok</li>`);
    if (g.tasks?.youtube_follow) reqs.push(`<li>▶️ Subscribe YouTube</li>`);
    if (g.tasks?.roles?.length) reqs.push(`<li>🏅 Roles: ${escapeHtml(g.tasks.roles.join(', '))}</li>`);
    if (g.tasks?.manual_task) reqs.push(`<li>📝 ${escapeHtml(g.tasks.manual_task)}</li>`);

    return `
      <div class="g-card">
        ${g.banner_url ? `<img src="${escapeHtml(g.banner_url)}" class="g-card-banner" alt="banner">` : ''}
        <div class="g-card-body">
          <div class="g-host-info">
            <div class="g-host-icon">👑</div>
            <span>Hosted by <b>${escapeHtml(g.hosted_by || 'Admin')}</b></span>
          </div>

          <h3 class="g-title">${escapeHtml(g.title)}</h3>
          <p class="g-desc">${escapeHtml(g.description)}</p>

          <div class="g-badge-container">
            ${g.guaranteed_spots ? `<span class="g-badge g-badge-guaranteed">💎 ${g.guaranteed_spots} Guaranteed</span>` : ''}
            ${g.fcfs_spots ? `<span class="g-badge g-badge-fcfs">⚡ ${g.fcfs_spots} FCFS</span>` : ''}
            ${isEnded ? '<span class="g-badge g-badge-ended">🔒 Ended</span>' : `<span class="g-badge g-badge-timer">⏳ ${timeLeft}</span>`}
          </div>

          <div class="g-tasks-summary">
            <div class="g-tasks-title">Requirements</div>
            <ul class="g-task-list">
              ${reqs.slice(0, 4).join('')}
              ${reqs.length > 4 ? `<li style="font-style: italic; font-size: 0.78rem;">+ ${reqs.length - 4} more requirements</li>` : ''}
            </ul>
          </div>
        </div>

        <div class="g-card-footer">
          <span style="font-size: 0.85rem; color: var(--text-muted);">👥 ${g.entries_count || 0} Entered</span>
          <button class="btn btn-primary btn-sm" onclick="openDetailModal('${g.id}')">
            ${isEnded ? 'View Results' : 'View Giveaway'}
          </button>
        </div>
      </div>
    `;
  }).join('');
}

// Load Guild Channels for Channel Selector
async function loadGuildChannels() {
  const select = document.getElementById('gChannel');
  select.innerHTML = '<option value="">Loading channels...</option>';
  try {
    const res = await fetch(apiUrl('/api/guilds'), { credentials: 'include' });
    const channels = await res.json();
    if (channels.length === 0) {
      select.innerHTML = '<option value="">No available channels found</option>';
      return;
    }
    select.innerHTML = channels.map(c => `
      <option value="${c.id}">#${escapeHtml(c.name)} (${escapeHtml(c.guild_name)})</option>
    `).join('');
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load channels</option>';
  }
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
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">🗑️</button>
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
    typeBadge = '🐦 Follow';
    placeholder = 'Handle (e.g. @WizardX_0x)';
  } else if (type === 'twitter_like') {
    typeBadge = '❤️ Like';
    placeholder = 'Tweet Link / URL';
  } else if (type === 'twitter_retweet') {
    typeBadge = '🔄 Retweet';
    placeholder = 'Tweet Link / URL';
  } else if (type === 'tiktok_follow') {
    typeBadge = '🎵 TikTok';
    placeholder = 'TikTok Handle / Link';
  } else if (type === 'youtube_follow') {
    typeBadge = '▶️ YouTube';
    placeholder = 'Channel Link / Name';
  } else if (type === 'role_require') {
    typeBadge = '🏅 Role';
    placeholder = 'Required Server Role Name';
  } else {
    typeBadge = '📝 Custom';
    placeholder = 'Task instructions...';
  }

  div.innerHTML = `
    <span class="g-badge g-badge-fcfs" style="min-width: 90px; text-align: center;">${typeBadge}</span>
    <input type="text" class="form-input dynamic-task-val" data-type="${type}" value="${escapeHtml(defaultVal)}" placeholder="${placeholder}" style="flex: 1; padding: 6px 10px; font-size: 0.85rem;">
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">🗑️</button>
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

// Submit Create Giveaway
async function submitCreateGiveaway() {
  const title = document.getElementById('gTitle').value.trim();
  const description = document.getElementById('gDesc').value.trim();
  const banner_url = document.getElementById('gBanner').value.trim();
  const channel_id = document.getElementById('gChannel').value;
  const spot_tiers = getSpotTiersPayload();
  const min_per_user = parseInt(document.getElementById('gMinPerUser').value) || 1;
  const max_per_user = parseInt(document.getElementById('gMaxPerUser').value) || 1;
  const duration_val = parseFloat(document.getElementById('gDurationVal').value) || 15;
  const duration_unit = document.getElementById('gDurationUnit').value;
  const network = document.getElementById('gNetwork').value.trim() || 'Ethereum';

  const dynamic_tasks = getDynamicTasksPayload();
  const require_evm = document.getElementById('reqEvm').checked;
  const require_solana = document.getElementById('reqSolana').checked;

  const giveawayId = 'g_' + Date.now();
  let durationInSeconds = duration_val * 60;
  if (duration_unit === 'hours') durationInSeconds = duration_val * 3600;
  if (duration_unit === 'days') durationInSeconds = duration_val * 86400;

  const giveawayObj = {
    id: giveawayId,
    title,
    description,
    banner_url,
    channel_id: channel_id || 'general',
    spot_tiers,
    min_per_user,
    max_per_user,
    duration_val,
    duration_unit,
    network,
    is_active: true,
    created_at: Math.floor(Date.now() / 1000),
    ends_at: Math.floor(Date.now() / 1000) + durationInSeconds,
    hosted_by: currentUser ? currentUser.username : 'Admin',
    guaranteed_spots: (spot_tiers.find(t => t.type === 'guaranteed') || {}).spots || 0,
    fcfs_spots: (spot_tiers.find(t => t.type === 'fcfs') || {}).spots || 0,
    entries_count: 0,
    tasks: {
      dynamic_tasks,
      require_evm,
      require_solana
    }
  };

  try {
    await firebasePut('giveaways/' + giveawayId, giveawayObj);
    showToast('🚀 Giveaway created successfully!', 'success');
    closeModal('createModal');
    await loadGiveaways();
  } catch (err) {
    showToast('Error creating giveaway', 'error');
  }
}

// Open Detail & Admin Verification Modal
async function openDetailModal(giveawayId) {
  const g = currentGiveaways.find(x => x.id === giveawayId);
  if (!g) return;

  activeDetailGiveaway = g;
  document.getElementById('detailTitle').innerText = g.title;
  
  const content = document.getElementById('detailContent');
  const now = Math.floor(Date.now() / 1000);
  const isEnded = !g.is_active || g.ends_at <= now;

  // Build task requirements list for public view
  const reqs = [];
  if (g.tasks?.twitter_follow) reqs.push(`<li>🐦 Follow <b>@${escapeHtml(g.tasks.twitter_follow)}</b></li>`);
  if (g.tasks?.twitter_like) reqs.push(`<li>❤️ Like Tweet</li>`);
  if (g.tasks?.twitter_retweet) reqs.push(`<li>🔄 Retweet Tweet</li>`);
  if (g.tasks?.tiktok_follow) reqs.push(`<li>🎵 Follow TikTok</li>`);
  if (g.tasks?.youtube_follow) reqs.push(`<li>▶️ Subscribe YouTube</li>`);
  if (g.tasks?.roles?.length) reqs.push(`<li>🏅 Required Roles: ${escapeHtml(g.tasks.roles.join(', '))}</li>`);
  if (g.tasks?.manual_task) reqs.push(`<li>📝 ${escapeHtml(g.tasks.manual_task)}</li>`);

  content.innerHTML = `
    <div style="display: flex; flex-direction: column; gap: 1rem;">
      ${g.banner_url ? `<img src="${escapeHtml(g.banner_url)}" style="width: 100%; height: 220px; object-fit: cover; border-radius: var(--radius-md);" alt="banner">` : ''}
      <p style="font-size: 1rem; color: var(--text-muted);">${escapeHtml(g.description)}</p>
      
      <div class="g-badge-container">
        <span class="g-badge g-badge-guaranteed">💎 ${g.guaranteed_spots} Guaranteed Spots</span>
        <span class="g-badge g-badge-fcfs">⚡ ${g.fcfs_spots} FCFS Spots</span>
        <span class="g-badge g-badge-timer">🌐 Network: ${escapeHtml(g.network || 'Ethereum')}</span>
        ${isEnded ? '<span class="g-badge g-badge-ended">Ended</span>' : `<span class="g-badge g-badge-timer">Ends ${getTimeLeftString(g.ends_at)}</span>`}
      </div>

      <div class="g-tasks-summary" style="margin-top: 10px;">
        <div class="g-tasks-title">Giveaway Task Requirements</div>
        <ul class="g-task-list" style="font-size: 0.9rem; gap: 6px;">
          ${reqs.length ? reqs.join('') : '<li>No extra requirements specified.</li>'}
        </ul>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; pt-2;">
        <button class="btn btn-outline btn-sm" onclick="copyShareLink('${g.id}')">🔗 Share Giveaway Link</button>
        ${!currentUser ? '<span style="font-size: 0.8rem; color: var(--text-muted);">Sign in with Discord to participate!</span>' : ''}
      </div>
    </div>
  `;

  // Admin Box setup
  const adminBox = document.getElementById('adminControlBox');
  if (currentUser && currentUser.is_admin) {
    adminBox.style.display = 'block';
    await loadGiveawayParticipants(giveawayId);
    
    document.getElementById('drawWinnersBtn').onclick = () => drawWinners(giveawayId);
    document.getElementById('redrawWinnersBtn').onclick = () => redrawWinners(giveawayId);
    document.getElementById('exportWinnersBtn').onclick = () => exportWinnersCSV(giveawayId);
  } else {
    adminBox.style.display = 'none';
  }

  openModal('detailModal');
}

function copyShareLink(giveawayId) {
  const shareUrl = `${window.location.origin}/?giveaway=${giveawayId}`;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(shareUrl).then(() => {
      showToast('📋 Share link copied to clipboard!', 'success');
    }).catch(() => {
      prompt('Copy share link:', shareUrl);
    });
  } else {
    prompt('Copy share link:', shareUrl);
  }
}

// Load Participants into Admin Table
async function loadGiveawayParticipants(giveawayId) {
  const tbody = document.getElementById('participantsTableBody');
  tbody.innerHTML = '<tr><td colspan="6">Loading entries...</td></tr>';
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
    const data = await res.json();
    const entries = data.entries || [];
    
    if (entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No entries recorded yet.</td></tr>';
      return;
    }

    tbody.innerHTML = entries.map(e => `
      <tr>
        <td>
          <b>${escapeHtml(e.username || 'User')}</b><br>
          <span style="font-size: 0.75rem; color: var(--text-dim);">ID: ${e.user_id}</span>
        </td>
        <td>
          ${e.winner_type ? `<span class="g-badge ${e.winner_type === 'guaranteed' ? 'g-badge-guaranteed' : 'g-badge-fcfs'}">${e.winner_type.toUpperCase()} WINNER</span>` : '<span style="color: var(--text-muted);">Entered</span>'}
        </td>
        <td><code>${escapeHtml(e.evm_wallet || 'None')}</code></td>
        <td><code>${escapeHtml(e.solana_wallet || 'None')}</code></td>
        <td>
          <span style="font-size: 0.8rem;">
            🐦 ${escapeHtml(e.twitter || '-')}<br>
            ✈️ ${escapeHtml(e.telegram || '-')}
          </span>
        </td>
        <td>
          <select onchange="updateVerificationStatus('${giveawayId}', '${e.user_id}', this.value)" class="form-select" style="padding: 4px 8px; font-size: 0.8rem;">
            <option value="pending" ${e.task_status === 'pending' ? 'selected' : ''}>🟡 Pending</option>
            <option value="verified" ${e.task_status === 'verified' ? 'selected' : ''}>🟢 Verified</option>
            <option value="ineligible" ${e.task_status === 'ineligible' ? 'selected' : ''}>🔴 Ineligible</option>
          </select>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = '<tr><td colspan="6">Error loading entries</td></tr>';
  }
}

// Admin Winner Drawing
async function drawWinners(giveawayId) {
  if (!confirm('Are you sure you want to draw/assign winners for this giveaway?')) return;
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/draw`), { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) {
      showToast(`🎉 Selected ${data.guaranteed_winners_count} Guaranteed & ${data.fcfs_winners_count} FCFS winners!`, 'success');
      await loadGiveawayParticipants(giveawayId);
      await loadGiveaways();
    } else {
      showToast(data.error || 'Failed to draw winners', 'error');
    }
  } catch (err) {
    showToast('Error drawing winners', 'error');
  }
}

// Admin Winner Re-Drawing (Re-Raffle Disqualified Spots)
async function redrawWinners(giveawayId) {
  if (!confirm('Are you sure you want to re-raffle replacement winners for any disqualified spots?')) return;
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/redraw`), { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) {
      showToast(`🔄 Re-raffled! Selected ${data.new_guaranteed_count} new Guaranteed & ${data.new_fcfs_count} new FCFS winners!`, 'success');
      await loadGiveawayParticipants(giveawayId);
      await loadGiveaways();
    } else {
      showToast(data.error || 'Failed to re-raffle winners', 'error');
    }
  } catch (err) {
    showToast('Error re-raffling winners', 'error');
  }
}

// Update Verification Status
async function updateVerificationStatus(giveawayId, userId, status) {
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/verify-winner`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ user_id: userId, task_status: status })
    });
    if (res.ok) {
      showToast(`Updated status to ${status}`, 'info');
    } else {
      showToast('Failed to update status', 'error');
    }
  } catch (err) {
    showToast('Error updating status', 'error');
  }
}

// Export Winners as CSV
async function exportWinnersCSV(giveawayId) {
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
    const data = await res.json();
    const winners = (data.entries || []).filter(e => e.winner_type);
    
    if (winners.length === 0) {
      showToast('No winners to export yet.', 'info');
      return;
    }

    let csv = 'Discord Tag,Discord ID,Spot Type,EVM Wallet,Solana Wallet,Twitter,Telegram,Task Status\n';
    winners.forEach(w => {
      csv += `"${w.username}","${w.user_id}","${w.winner_type}","${w.evm_wallet || ''}","${w.solana_wallet || ''}","${w.twitter || ''}","${w.telegram || ''}","${w.task_status || ''}"\n`;
    });

    const blob = new Blob([csv], { type: 'text/csv' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `giveaway_${giveawayId}_winners.csv`;
    a.click();
  } catch (err) {
    showToast('Failed to export winners', 'error');
  }
}

// User Profile Modal Setup & Save
function openProfileModal() {
  if (!currentUser) return;
  document.getElementById('profTwitter').value = currentUser.twitter || '';
  document.getElementById('profTelegram').value = currentUser.telegram || '';
  document.getElementById('profEvm').value = currentUser.evm_wallet || '';
  document.getElementById('profSolana').value = currentUser.solana_wallet || '';
  openModal('profileModal');
}

async function submitSaveProfile() {
  const twitter = document.getElementById('profTwitter').value.trim();
  const telegram = document.getElementById('profTelegram').value.trim();
  const evm_wallet = document.getElementById('profEvm').value.trim();
  const solana_wallet = document.getElementById('profSolana').value.trim();

  try {
    const res = await fetch(apiUrl('/api/user/profile'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ twitter, telegram, evm_wallet, solana_wallet })
    });
    if (res.ok) {
      showToast('Profile and wallets updated!', 'success');
      closeModal('profileModal');
      await checkAuth();
    } else {
      showToast('Failed to update profile', 'error');
    }
  } catch (err) {
    showToast('Error saving profile', 'error');
  }
}

// Admin Password Login
async function submitPasswordLogin(e) {
  e.preventDefault();
  const username = document.getElementById('passUser').value.trim();
  const password = document.getElementById('passWord').value.trim();
  
  try {
    const res = await fetch(apiUrl('/api/auth/password-login'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if (res.ok) {
      showToast('🚀 Signed in as Admin!', 'success');
      closeModal('passLoginModal');
      await checkAuth();
      await loadGiveaways();
    } else {
      showToast(data.error || 'Invalid credentials', 'error');
    }
  } catch (err) {
    showToast('Error signing in', 'error');
  }
}

// Utility Modal Helpers
function openModal(id) {
  document.getElementById(id).classList.add('active');
}
function closeModal(id) {
  document.getElementById(id).classList.remove('active');
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
