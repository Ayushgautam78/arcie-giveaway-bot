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

// Helper: Firebase REST read
async function firebaseGet(path) {
  const res = await fetch(`${FIREBASE_DB}/${path}.json`);
  return await res.json();
}

// Helper: Firebase REST write
async function firebasePut(path, data) {
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

  await fetch(`${FIREBASE_DB}/${path}.json`, {
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
    return `<a href="${url}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: underline; font-weight: 600;">${label} 🔗</a>`;
  });

  // 2. Raw URLs (not already inside href="...")
  str = str.replace(/(^|[^"])((https?:\/\/[^\s<]+))/g, (match, prefix, fullUrl) => {
    if (prefix.includes('href=') || prefix.includes('src=')) return match;
    return `${prefix}<a href="${fullUrl}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: underline; font-weight: 600;">Click Here 🔗</a>`;
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

// Render Social Links HTML Buttons for Web Display
function renderSocialButtonsHTML(social_links) {
  if (!social_links || typeof social_links !== 'object') return '';
  const btns = [];
  if (social_links.twitter_link && social_links.twitter_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.twitter_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 4px 10px; font-size: 0.8rem; border-color: rgba(29,161,242,0.4); color: #38bdf8;">🐦 Twitter / X</a>`);
  }
  if (social_links.discord_link && social_links.discord_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.discord_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 4px 10px; font-size: 0.8rem; border-color: rgba(88,101,242,0.4); color: #818cf8;">💬 Discord</a>`);
  }
  if (social_links.telegram_link && social_links.telegram_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.telegram_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 4px 10px; font-size: 0.8rem; border-color: rgba(0,136,204,0.4); color: #38bdf8;">✈️ Telegram</a>`);
  }
  if (social_links.website_link && social_links.website_link.startsWith('http')) {
    btns.push(`<a href="${escapeHtml(social_links.website_link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline btn-sm" style="padding: 4px 10px; font-size: 0.8rem; border-color: rgba(168,85,247,0.4); color: #c084fc;">🌐 Website</a>`);
  }
  if (!btns.length) return '';
  return `<div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px;">${btns.join('')}</div>`;
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

// Load Guild Channels for Channel Selector from Firebase
async function loadGuildChannels() {
  try {
    const channels = await firebaseGet('channels');
    let channelArray = [];
    if (channels && typeof channels === 'object') {
      channelArray = Array.isArray(channels) ? channels : Object.values(channels);
    }

    if (channelArray.length > 0) {
      const options = channelArray.map(c =>
        `<option value="${c.id}">💬 #${escapeHtml(c.name)}  •  ${escapeHtml(c.guild_name || 'Server')}</option>`
      ).join('');

      const gCh = document.getElementById('gChannel');
      if (gCh) gCh.innerHTML = '<option value="auto">⚡ Auto-Detect Main Channel</option>' + options;

      const gWin = document.getElementById('gWinnerChannel');
      if (gWin) gWin.innerHTML = '<option value="">📢 Same as Giveaway Channel (Default)</option>' + options;

      const editGWin = document.getElementById('editGWinnerChannel');
      if (editGWin) editGWin.innerHTML = '<option value="">📢 Same as Giveaway Channel (Default)</option>' + options;

      const editGCh = document.getElementById('editGChannel');
      if (editGCh) editGCh.innerHTML = '<option value="">-- Same as current channel --</option>' + options;
    } else {
      const gCh = document.getElementById('gChannel');
      if (gCh) gCh.innerHTML = '<option value="auto">⚡ Auto-Detect Main Channel</option>';
    }
  } catch (err) {
    console.error('Failed to load channels:', err);
  }
}

// Load Guild Roles for Mention Role dropdowns from Firebase
async function loadGuildRoles() {
  try {
    const roles = await firebaseGet('roles');
    let roleArray = [];
    if (roles && typeof roles === 'object') {
      roleArray = Array.isArray(roles) ? roles : Object.values(roles);
    }

    const uniqueRoles = [];
    const seenIds = new Set();
    roleArray.forEach(r => {
      if (r && r.id && !seenIds.has(r.id)) {
        seenIds.add(r.id);
        uniqueRoles.push(r);
      }
    });

    const roleOpts = uniqueRoles
      .filter(r => r.id !== '@everyone')
      .map(r => `<option value="${r.id}">🏷️ @${escapeHtml(r.name)}  •  ${escapeHtml(r.guild_name || 'Server')}</option>`)
      .join('');

    const baseOptions = `
      <option value="">🔕 No Ping (Silent Announcement)</option>
      <option value="@everyone">🌐 @everyone (Ping Entire Server)</option>
      <option value="@here">⚡ @here (Ping Online Members Only)</option>
    `;

    const gRole = document.getElementById('gMentionRole');
    if (gRole && gRole.tagName === 'SELECT') {
      gRole.innerHTML = baseOptions + (roleOpts ? `<optgroup label="Server Roles">${roleOpts}</optgroup>` : '');
    }

    const editRole = document.getElementById('editGMentionRole');
    if (editRole && editRole.tagName === 'SELECT') {
      editRole.innerHTML = baseOptions + (roleOpts ? `<optgroup label="Server Roles">${roleOpts}</optgroup>` : '');
    }

    // Dedicated Required Roles Dropdown options
    cachedServerRoles = uniqueRoles.filter(r => r.id !== '@everyone');
    const reqRoleOpts = '<option value="">Select Discord Server Role...</option>' + cachedServerRoles
      .map(r => `<option value="${r.id}" data-name="${escapeHtml(r.name)}">@${escapeHtml(r.name)} (${escapeHtml(r.guild_name || 'Server')})</option>`)
      .join('');

    const gReqRole = document.getElementById('gReqRoleSelect');
    if (gReqRole && gReqRole.tagName === 'SELECT') {
      gReqRole.innerHTML = reqRoleOpts;
    }

    const editReqRole = document.getElementById('editGReqRoleSelect');
    if (editReqRole && editReqRole.tagName === 'SELECT') {
      editReqRole.innerHTML = reqRoleOpts;
    }
  } catch (err) {
    console.error('Failed to load roles:', err);
  }
}

// -------- Required Role Chip Management (OR Logic) -------- //
let cachedServerRoles = [];
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
      🏷️ @${escapeHtml(role.name || role.id)}
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
      🏷️ @${escapeHtml(role.name || role.id)}
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
    showToast('✅ Backup downloaded successfully!', 'success');
  } catch (err) {
    console.error('Backup download error:', err);
    showToast('❌ Failed to download backup: ' + err.message, 'error');
  }
}

// Restore Backup JSON
async function handleRestoreBackup(input) {
  const file = input.files && input.files[0];
  if (!file) return;

  if (!confirm('⚠️ WARNING: Restoring a backup will overwrite existing giveaways, participant entries, and user profiles.\n\nAre you sure you want to proceed?')) {
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

      showToast('🎉 Backup restored successfully!', 'success');
      await loadGiveaways();
    } catch (err) {
      console.error('Restore error:', err);
      showToast('❌ Failed to restore backup: ' + err.message, 'error');
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

// Load Giveaways directly from Firebase with API fallback
async function loadGiveaways() {
  try {
    let data = null;
    try {
      data = await firebaseGet('giveaways');
    } catch (e) {
      console.warn('Firebase giveaways fetch failed, attempting API fallback:', e);
    }

    if (!data || typeof data !== 'object' || Object.keys(data).length === 0) {
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
        console.warn('Backend API giveaways fallback failed:', apiErr);
      }
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

// Render Giveaway Cards
function renderGiveaways() {
  const grid = document.getElementById('giveawayGrid');
  const now = Math.floor(Date.now() / 1000);
  const isAdmin = currentUser && currentUser.is_admin;

  let filtered = currentGiveaways;

  // Non-admin users: ONLY show active giveaways (no ended, no all tab)
  if (!isAdmin) {
    filtered = currentGiveaways.filter(g => g.is_active && g.ends_at > now);
  } else {
    if (currentFilter === 'active') {
      filtered = currentGiveaways.filter(g => g.is_active && g.ends_at > now);
    } else if (currentFilter === 'ended') {
      filtered = currentGiveaways.filter(g => !g.is_active || g.ends_at <= now);
    }
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
          <div class="g-desc">${formatMarkdownDescription(g.description)}</div>

          <div class="g-badge-container">
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
          <div style="display: flex; gap: 6px; align-items: center;">
            ${isAdmin ? `<button type="button" class="btn btn-danger btn-sm" style="padding: 4px 8px;" onclick="deleteGiveaway('${g.id}')" title="Delete Giveaway">🗑️</button>` : ''}
            <button class="btn btn-primary btn-sm" onclick="openDetailModal('${g.id}')">
              ${isEnded ? 'View Results' : 'View Giveaway'}
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
  } else if (type === 'twitter_comment') {
    typeBadge = '💬 Comment';
    placeholder = 'Tweet Link / URL to Comment';
  } else if (type === 'discord_join' || type === 'discord_server') {
    typeBadge = '💬 Discord';
    placeholder = 'https://discord.gg/invitecode or Server Name';
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
      showToast('📷 Banner image uploaded successfully!', 'success');
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
    showToast('📷 Image loaded!', 'success');
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
    btn.innerHTML = '⏳ Publishing...';
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
        await firebasePut('giveaway_entries/' + giveawayId, []);
        showToast('🚀 Giveaway published & posted to Discord!', 'success');
      } else {
        await firebasePut('giveaways/' + giveawayId, giveawayObj);
        await firebasePut('giveaway_entries/' + giveawayId, []);
        showToast('🚀 Giveaway created!', 'success');
      }
    } catch (err) {
      await firebasePut('giveaways/' + giveawayId, giveawayObj);
      await firebasePut('giveaway_entries/' + giveawayId, []);
      showToast('🚀 Giveaway created!', 'success');
    }

    createRequiredRoles = [];
    renderCreateRequiredRoles();
    closeModal('createModal');
    await loadGiveaways();
  } finally {
    isSubmittingCreate = false;
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '🚀 Publish Giveaway';
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
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">🗑️</button>
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
  if (type === 'twitter_follow') typeBadge = '🐦 Follow';
  else if (type === 'twitter_like') typeBadge = '❤️ Like';
  else if (type === 'twitter_retweet') typeBadge = '🔄 Retweet';
  else if (type === 'twitter_comment') typeBadge = '💬 Comment';
  else if (type === 'discord_join' || type === 'discord_server') typeBadge = '💬 Discord';
  else if (type === 'tiktok_follow') typeBadge = '🎵 TikTok';
  else if (type === 'youtube_follow') typeBadge = '▶️ YouTube';
  else if (type === 'role_require') typeBadge = '🏅 Role';
  else typeBadge = '📝 Custom';

  div.innerHTML = `
    <span class="g-badge g-badge-fcfs" style="min-width: 90px; text-align: center;">${typeBadge}</span>
    <input type="text" class="form-input edit-dynamic-task-val" data-type="${type}" value="${escapeHtml(defaultVal)}" placeholder="Requirement value..." style="flex: 1; padding: 6px 10px; font-size: 0.85rem;">
    <button type="button" class="btn btn-danger btn-sm" onclick="document.getElementById('${id}').remove()" style="padding: 4px 8px;">🗑️</button>
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
      showToast('✏️ Giveaway updated successfully!', 'success');
    } catch (err) {
      await firebasePut(`giveaways/${gId}`, g);
      showToast('✏️ Giveaway updated!', 'success');
    }

    closeModal('editModal');
    closeModal('detailModal');
    await loadGiveaways();
  } finally {
    isSubmittingEdit = false;
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '💾 Save Changes';
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
  showToast('🗑️ Giveaway permanently deleted!', 'success');

  closeModal('detailModal');
  closeModal('editModal');
  await loadGiveaways();
}

// Open Detail & Admin Verification Modal
async function openDetailModal(giveawayId) {
  const g = currentGiveaways.find(x => x.id === giveawayId);
  if (!g) return;

  const isAdmin = currentUser && currentUser.is_admin;
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
  if (g.tasks?.dynamic_tasks) {
    g.tasks.dynamic_tasks.forEach(t => {
      reqs.push(`<li>📝 ${escapeHtml(t.value)}</li>`);
    });
  }

  content.innerHTML = `
    <div style="display: flex; flex-direction: column; gap: 1rem;">
      ${g.banner_url ? `<img src="${escapeHtml(g.banner_url)}" style="width: 100%; height: 220px; object-fit: cover; border-radius: var(--radius-md);" alt="banner">` : ''}
      <div style="font-size: 0.98rem; color: var(--text-main); line-height: 1.6; background: rgba(0,0,0,0.25); padding: 1rem; border-radius: var(--radius-sm); border: 1px solid var(--border-color);">${formatMarkdownDescription(g.description)} ${renderSocialButtonsHTML(g.social_links)}</div>
      
      <div class="g-badge-container">
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

  // Public participants list (visible to everyone)
  await loadPublicParticipants(giveawayId, g.network || 'Ethereum');

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

// Determine which wallet field to show based on network name
function getWalletFieldForNetwork(network) {
  const n = (network || '').toLowerCase().trim();
  if (n === 'solana' || n === 'sol') return { field: 'solana_wallet', label: 'Solana Wallet' };
  // All EVM-compatible chains
  return { field: 'evm_wallet', label: 'Wallet Address' };
}

// Load Public Participants (visible to everyone)
async function loadPublicParticipants(giveawayId, network) {
  const tbody = document.getElementById('publicParticipantsBody');
  const walletHeader = document.getElementById('publicWalletHeader');
  if (!tbody) return;

  const walletInfo = getWalletFieldForNetwork(network);
  if (walletHeader) walletHeader.innerText = walletInfo.label;

  tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; color: var(--text-muted); padding: 1rem;">Loading participants...</td></tr>';

  try {
    let entries = [];

    // 1. Try Firebase directly
    const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
    if (fbData && typeof fbData === 'object') {
      entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
    }

    // 2. Fallback to backend API
    if (!entries || entries.length === 0) {
      try {
        const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
        if (res.ok) {
          const data = await res.json();
          entries = data.entries || [];
        }
      } catch (e) {
        console.warn('Backend API fallback unavailable:', e);
      }
    }

    if (!entries || entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No participants yet.</td></tr>';
      return;
    }

    tbody.innerHTML = entries.map(e => {
      if (!e) return '';
      const wallet = e[walletInfo.field] || 'Not provided';
      return `
        <tr>
          <td><b>${escapeHtml(e.username || e.display_name || 'User')}</b></td>
          <td><code style="font-size: 0.8rem;">${escapeHtml(e.user_id || 'N/A')}</code></td>
          <td><code style="font-size: 0.8rem;">${escapeHtml(wallet)}</code></td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    console.error('Error loading public participants:', err);
    tbody.innerHTML = '<tr><td colspan="3" style="text-align: center; color: #ff4757;">Failed to load participants.</td></tr>';
  }
}

// Load Participants into Admin Table with Winner Highlighting
async function loadGiveawayParticipants(giveawayId) {
  const tbody = document.getElementById('participantsTableBody');
  tbody.innerHTML = '<tr><td colspan="6">Loading entries...</td></tr>';
  try {
    let entries = [];
    
    // 1. Try reading directly from Firebase Cloud DB (works 100% on Vercel without CORS or server dependency)
    const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
    if (fbData && typeof fbData === 'object') {
      entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
    }

    // 2. Fallback to Python Backend API if Firebase is empty
    if (!entries || entries.length === 0) {
      try {
        const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
        if (res.ok) {
          const data = await res.json();
          entries = data.entries || [];
        }
      } catch (e) {
        console.warn('Backend API fallback unavailable:', e);
      }
    }
    
    if (!entries || entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No entries recorded yet. Users click [Join Giveaway] on Discord or website to participate!</td></tr>';
      return;
    }

    tbody.innerHTML = entries.map(e => {
      if (!e) return '';
      const isWinner = !!e.winner_type;
      const winnerBadge = isWinner 
        ? `<span class="g-badge ${String(e.winner_type).toLowerCase().includes('guarantee') ? 'g-badge-guaranteed' : 'g-badge-fcfs'}" style="font-weight: bold; padding: 3px 8px;">🏆 WINNER (${escapeHtml(String(e.winner_type).toUpperCase())})</span>`
        : '<span style="color: var(--text-muted);">Participant</span>';
      
      const nameStyle = isWinner ? 'color: #ffd700; font-weight: bold;' : 'font-weight: bold;';

      return `
        <tr style="${isWinner ? 'background: rgba(255, 215, 0, 0.08);' : ''}">
          <td>
            <b style="${nameStyle}">${escapeHtml(e.username || e.display_name || 'User')}</b> ${isWinner ? '🏆' : ''}<br>
            <span style="font-size: 0.75rem; color: var(--text-dim);">ID: ${e.user_id || 'N/A'}</span>
          </td>
          <td>${winnerBadge}</td>
          <td><code>${escapeHtml(e.evm_wallet || 'None')}</code></td>
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
                <option value="verified" ${e.task_status === 'verified' || !e.task_status ? 'selected' : ''}>🟢 Verified</option>
                <option value="pending" ${e.task_status === 'pending' ? 'selected' : ''}>🟡 Pending</option>
                <option value="ineligible" ${e.task_status === 'ineligible' ? 'selected' : ''}>🔴 Ineligible</option>
              </select>
              <button type="button" class="btn btn-danger btn-sm" style="padding: 3px 7px; font-size: 0.8rem;" onclick="deleteParticipantEntry('${giveawayId}', '${e.user_id}')" title="Delete Entry">🗑️</button>
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
async function drawWinners(giveawayId) {
  if (!confirm('Are you sure you want to draw/assign winners for this giveaway?')) return;
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/draw`), { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) {
      showToast(`🎉 Winners selected! Announcement posted to Discord!`, 'success');
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
      showToast(`🔄 Replacement winners re-raffled & posted to Discord!`, 'success');
      await loadGiveawayParticipants(giveawayId);
      await loadGiveaways();
    } else {
      showToast(data.error || 'Failed to re-raffle winners', 'error');
    }
  } catch (err) {
    showToast('Error re-raffling winners', 'error');
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
    showToast('🗑️ Participant entry removed!', 'success');
    await loadGiveawayParticipants(giveawayId);
    await loadGiveaways();
  } catch (err) {
    showToast('Error deleting entry', 'error');
  }
}

// Admin: Send Winners Announcement manually
async function sendWinnersAnnouncement(giveawayId) {
  try {
    const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}/announce`), { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) {
      showToast('📢 Winners Announcement posted directly to Discord!', 'success');
    } else {
      showToast(data.error || 'Failed to post announcement', 'error');
    }
  } catch (err) {
    showToast('Error sending announcement', 'error');
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

// Export All Entries as CSV (Reads directly from Firebase over HTTPS)
async function exportAllEntriesCSV(giveawayId) {
  try {
    let entries = [];
    const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
    if (fbData && typeof fbData === 'object') {
      entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
    }

    if (!entries || entries.length === 0) {
      try {
        const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
        const data = await res.json();
        entries = data.entries || [];
      } catch (e) {}
    }

    if (!entries || entries.length === 0) {
      showToast('No entries recorded yet to download.', 'info');
      return;
    }

    let csv = '\uFEFFDiscord Username,Discord ID,Twitter Handle,Telegram Handle,EVM Wallet,Solana Wallet,Task Status,Winner Status\n';
    entries.forEach(e => {
      if (!e) return;
      const winnerStatus = e.winner_type ? `WINNER (${String(e.winner_type).toUpperCase()})` : 'Participant';
      csv += `"${(e.username || e.display_name || 'User').replace(/"/g, '""')}","${e.user_id || ''}","${(e.twitter || '').replace(/"/g, '""')}","${(e.telegram || '').replace(/"/g, '""')}","${e.evm_wallet || ''}","${e.solana_wallet || ''}","${e.task_status || 'verified'}","${winnerStatus}"\n`;
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

    showToast('📥 Exported all entries to CSV!', 'success');
  } catch (err) {
    console.error('CSV export error:', err);
    showToast('Failed to export entries', 'error');
  }
}

// Export Winners as CSV (Reads directly from Firebase over HTTPS)
async function exportWinnersCSV(giveawayId) {
  try {
    let entries = [];
    const fbData = await firebaseGet('giveaway_entries/' + giveawayId);
    if (fbData && typeof fbData === 'object') {
      entries = Array.isArray(fbData) ? fbData : Object.values(fbData);
    }

    if (!entries || entries.length === 0) {
      try {
        const res = await fetch(apiUrl(`/api/giveaways/${giveawayId}`), { credentials: 'include' });
        const data = await res.json();
        entries = data.entries || [];
      } catch (e) {}
    }

    const winners = entries.filter(e => e && e.winner_type);
    
    if (winners.length === 0) {
      showToast('No winners to export yet.', 'info');
      return;
    }

    let csv = '\uFEFFDiscord Username,Discord ID,Spot Type,EVM Wallet,Solana Wallet,Twitter Handle,Telegram Handle,Task Status\n';
    winners.forEach(w => {
      csv += `"${(w.username || w.display_name || 'User').replace(/"/g, '""')}","${w.user_id || ''}","${String(w.winner_type).toUpperCase()}","${w.evm_wallet || ''}","${w.solana_wallet || ''}","${(w.twitter || '').replace(/"/g, '""')}","${(w.telegram || '').replace(/"/g, '""')}","${w.task_status || 'verified'}"\n`;
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

    showToast('🏆 Exported winners to CSV!', 'success');
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
      showToast('🏆 Custom winners set and announced to Discord!', 'success');
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
