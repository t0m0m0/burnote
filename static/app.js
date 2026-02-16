// ---------------------------------------------------------------------------
// E2EE: AES-256-GCM via Web Crypto API
// ---------------------------------------------------------------------------
function arrayBufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}
function base64ToArrayBuf(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}
function arrayBufToBase64url(buf) {
  return arrayBufToBase64(buf).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function base64urlToArrayBuf(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  return base64ToArrayBuf(s);
}
async function generateEncryptionKey() {
  return crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
}
async function exportKeyToBase64url(key) {
  const raw = await crypto.subtle.exportKey('raw', key);
  return arrayBufToBase64url(raw);
}
async function importKeyFromBase64url(b64url) {
  const raw = base64urlToArrayBuf(b64url);
  return crypto.subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['decrypt']);
}
async function encryptContent(plaintext, key) {
  const encoder = new TextEncoder();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, encoder.encode(plaintext));
  const combined = new Uint8Array(iv.length + new Uint8Array(ciphertext).length);
  combined.set(iv);
  combined.set(new Uint8Array(ciphertext), iv.length);
  return arrayBufToBase64(combined.buffer);
}
async function decryptContent(b64data, key) {
  const combined = new Uint8Array(base64ToArrayBuf(b64data));
  const iv = combined.slice(0, 12);
  const ciphertext = combined.slice(12);
  const plainBuf = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, key, ciphertext);
  return new TextDecoder().decode(plainBuf);
}

// ---------------------------------------------------------------------------
// E2EE: Binary encrypt/decrypt for file attachments
// ---------------------------------------------------------------------------
async function encryptBinary(arrayBuffer, key) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, arrayBuffer);
  const combined = new Uint8Array(iv.length + new Uint8Array(ciphertext).length);
  combined.set(iv);
  combined.set(new Uint8Array(ciphertext), iv.length);
  return arrayBufToBase64(combined.buffer);
}
async function decryptBinary(b64data, key) {
  const combined = new Uint8Array(base64ToArrayBuf(b64data));
  const iv = combined.slice(0, 12);
  const ciphertext = combined.slice(12);
  return await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, key, ciphertext);
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentNoteId = null;
let currentKeyB64url = null;
let selectedFile = null;
let decryptedAttachment = null; // { blob, name, type }
let activeObjectUrls = [];

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
function init() {
  const path = window.location.pathname;
  const match = path.match(/^\/note\/([a-f0-9]+)$/);
  if (match) {
    currentNoteId = match[1];
    currentKeyB64url = window.location.hash.slice(1);
    showReadView();
  } else {
    showCreateView();
  }
}

function showCreateView() {
  document.getElementById('createView').style.display = 'block';
  document.getElementById('readView').classList.remove('show');
  document.getElementById('readView').style.display = 'none';
}

function showReadView() {
  document.getElementById('createView').style.display = 'none';
  document.getElementById('readView').classList.add('show');
  checkAndFetchNote();
}

// ---------------------------------------------------------------------------
// Create Note
// ---------------------------------------------------------------------------
async function createNote() {
  const content = document.getElementById('noteContent').value.trim();
  if (!content && !selectedFile) { document.getElementById('noteContent').focus(); return; }

  const btn = document.getElementById('createBtn');
  btn.disabled = true;
  btn.textContent = '⏳ 暗号化 & 作成中...';

  try {
    const key = await generateEncryptionKey();
    const keyB64url = await exportKeyToBase64url(key);

    // Encrypt text (if any)
    const encrypted = content ? await encryptContent(content, key) : '';

    // Encrypt file attachment (if any)
    let encAttachmentData = undefined;
    let encAttachmentMeta = undefined;
    if (selectedFile) {
      const progressEl = document.getElementById('uploadProgress');
      const progressBar = document.getElementById('uploadProgressBar');
      const progressText = document.getElementById('uploadProgressText');
      progressEl.style.display = 'block';
      // Simulated progress (not actual bytes — crypto operations are not streaming)
      progressBar.style.width = '20%';
      progressText.textContent = 'ファイルを読み込み中...';

      const fileBuf = await selectedFile.arrayBuffer();
      progressBar.style.width = '50%';
      progressText.textContent = '暗号化中...';

      encAttachmentData = await encryptBinary(fileBuf, key);
      progressBar.style.width = '80%';

      const meta = JSON.stringify({ name: selectedFile.name, type: selectedFile.type, size: selectedFile.size });
      encAttachmentMeta = await encryptContent(meta, key);
      progressBar.style.width = '100%';
      progressText.textContent = '送信中...';
    }

    const burnToggle = document.getElementById('burnToggle').checked;
    const maxReads = parseInt(document.getElementById('maxReads').value);
    const password = document.getElementById('notePassword').value || undefined;

    const body = {
      content: encrypted,
      burn_after_read: burnToggle,
      expires_minutes: parseInt(document.getElementById('expiry').value),
      max_reads: burnToggle ? 0 : maxReads,
      password: password,
    };
    if (encAttachmentData) {
      body.attachment_data = encAttachmentData;
      body.attachment_meta = encAttachmentMeta;
    }

    const res = await fetch('/api/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);

    const noteUrl = window.location.origin + '/note/' + data.id + '#' + keyB64url;
    document.getElementById('noteUrl').value = noteUrl;
    document.getElementById('burnWarning').style.display = burnToggle ? 'flex' : 'none';

    // QR Code
    const qrSection = document.getElementById('qrSection');
    qrSection.innerHTML = '';
    try {
      const qr = qrcode(0, 'L');
      qr.addData(noteUrl);
      qr.make();
      const img = qr.createImgTag(4, 8);
      const div = document.createElement('div');
      div.innerHTML = img;
      const imgEl = div.querySelector('img');
      imgEl.style.borderRadius = '8px';
      imgEl.style.background = '#fff';
      imgEl.style.padding = '8px';
      qrSection.appendChild(imgEl);
      const label = document.createElement('span');
      label.style.fontSize = '0.78rem';
      label.style.color = 'var(--text-dim)';
      label.textContent = 'QRコードで共有';
      qrSection.appendChild(label);
    } catch(e) { /* QR generation failed, skip */ }

    document.getElementById('resultCard').classList.add('show');
    document.getElementById('noteContent').value = '';
    removeAttachment(null);
    document.getElementById('uploadProgress').style.display = 'none';
  } catch (e) {
    alert('エラー: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '🔒 秘密のメモを作成';
    document.getElementById('uploadProgress').style.display = 'none';
  }
}

// ---------------------------------------------------------------------------
// Copy URL
// ---------------------------------------------------------------------------
function copyUrl() {
  const input = document.getElementById('noteUrl');
  navigator.clipboard.writeText(input.value).then(() => {
    const btn = document.getElementById('copyBtn');
    btn.textContent = '✅ コピー済';
    btn.style.borderColor = 'var(--success)';
    setTimeout(() => {
      btn.textContent = '📋 コピー';
      btn.style.borderColor = '';
    }, 2000);
  });
}

// ---------------------------------------------------------------------------
// Read Note — check password first, then fetch
// ---------------------------------------------------------------------------
async function checkAndFetchNote() {
  document.getElementById('readLoading').style.display = 'flex';
  document.getElementById('readContent').style.display = 'none';
  document.getElementById('readNotFound').style.display = 'none';

  try {
    await fetchNote(null);
  } catch (e) {
    if (e.message === 'password_required') {
      document.getElementById('readLoading').style.display = 'none';
      showPasswordModal();
    } else {
      document.getElementById('readLoading').style.display = 'none';
      document.getElementById('readNotFound').style.display = 'block';
    }
  }
}

function showPasswordModal() {
  document.getElementById('passwordModal').style.display = 'flex';
  document.getElementById('passwordError').style.display = 'none';
  document.getElementById('readPassword').value = '';
  setTimeout(() => document.getElementById('readPassword').focus(), 100);
}

async function submitPassword() {
  const password = document.getElementById('readPassword').value;
  if (!password) return;

  document.getElementById('submitPasswordBtn').disabled = true;
  document.getElementById('submitPasswordBtn').textContent = '⏳ 確認中...';

  try {
    await fetchNote(password);
    document.getElementById('passwordModal').style.display = 'none';
  } catch (e) {
    if (e.message === 'password_required') {
      document.getElementById('passwordError').style.display = 'block';
    } else {
      document.getElementById('passwordModal').style.display = 'none';
      document.getElementById('readNotFound').style.display = 'block';
    }
  } finally {
    document.getElementById('submitPasswordBtn').disabled = false;
    document.getElementById('submitPasswordBtn').textContent = '🔓 解錠';
  }
}

// Enter key for password
document.getElementById('readPassword').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') submitPassword();
});

async function fetchNote(password) {
  document.getElementById('readLoading').style.display = 'flex';
  document.getElementById('readContent').style.display = 'none';
  document.getElementById('readNoteInner').style.display = 'block';
  document.getElementById('ashesState').style.display = 'none';

  const headers = {};
  if (password) headers['X-Note-Password'] = password;

  await new Promise(r => setTimeout(r, 600));

  const res = await fetch('/api/notes/' + currentNoteId, { headers });

  if (res.status === 403) {
    document.getElementById('readLoading').style.display = 'none';
    throw new Error('password_required');
  }
  if (!res.ok) {
    document.getElementById('readLoading').style.display = 'none';
    document.getElementById('readNotFound').style.display = 'block';
    throw new Error('not_found');
  }

  const data = await res.json();

  // Import decryption key once (reused for text + attachment)
  let decryptionKey = null;
  if (currentKeyB64url) {
    decryptionKey = await importKeyFromBase64url(currentKeyB64url);
  }

  // Decrypt text content
  let plaintext = '';
  if (!currentKeyB64url) {
    plaintext = '⚠️ 復号キーがURLに含まれていません。リンクが不完全です。';
  } else if (data.content) {
    try {
      plaintext = await decryptContent(data.content, decryptionKey);
    } catch (decryptErr) {
      plaintext = '⚠️ 復号に失敗しました。キーが正しくないか、データが破損しています。';
    }
  }

  document.getElementById('readLoading').style.display = 'none';
  document.getElementById('readContent').style.display = 'block';

  // Show/hide text content
  const noteTextEl = document.getElementById('noteText');
  const textHeader = noteTextEl.previousElementSibling; // the header div
  if (plaintext) {
    noteTextEl.textContent = plaintext;
    noteTextEl.style.display = 'block';
    if (textHeader) textHeader.style.display = 'flex';
  } else {
    noteTextEl.style.display = 'none';
    if (textHeader) textHeader.style.display = 'none';
  }

  // Clean up previous object URLs
  activeObjectUrls.forEach(u => URL.revokeObjectURL(u));
  activeObjectUrls = [];

  // Decrypt and display attachment
  const attachmentView = document.getElementById('attachmentView');
  if (data.attachment_data && currentKeyB64url) {
    try {
      const metaJson = await decryptContent(data.attachment_meta, decryptionKey);
      const meta = JSON.parse(metaJson);

      const decryptedBuf = await decryptBinary(data.attachment_data, decryptionKey);
      const blob = new Blob([decryptedBuf], { type: meta.type });
      decryptedAttachment = { blob, name: meta.name, type: meta.type };

      const previewEl = document.getElementById('attachmentPreview');
      previewEl.innerHTML = '';
      const objUrl = URL.createObjectURL(blob);
      activeObjectUrls.push(objUrl);

      if (meta.type.startsWith('image/')) {
        const img = document.createElement('img');
        img.src = objUrl;
        img.alt = meta.name;
        img.loading = 'lazy';
        previewEl.appendChild(img);
      } else if (meta.type.startsWith('video/')) {
        const video = document.createElement('video');
        video.src = objUrl;
        video.controls = true;
        video.playsInline = true;
        video.preload = 'metadata';
        previewEl.appendChild(video);
      } else if (meta.type.startsWith('audio/')) {
        const audioWrapper = document.createElement('div');
        audioWrapper.className = 'audio-preview-wrapper';
        const icon = document.createElement('div');
        icon.className = 'audio-preview-icon';
        icon.textContent = '🎵';
        audioWrapper.appendChild(icon);
        const audio = document.createElement('audio');
        audio.src = objUrl;
        audio.controls = true;
        audio.preload = 'metadata';
        audioWrapper.appendChild(audio);
        previewEl.appendChild(audioWrapper);
      } else if (meta.type === 'application/pdf') {
        const iframe = document.createElement('iframe');
        iframe.src = objUrl;
        iframe.className = 'pdf-preview-frame';
        iframe.title = meta.name;
        previewEl.appendChild(iframe);
      } else {
        const fallbackDiv = document.createElement('div');
        fallbackDiv.className = 'file-fallback-preview';
        const iconSpan = document.createElement('span');
        iconSpan.className = 'file-fallback-icon';
        iconSpan.textContent = getFileTypeIcon(meta.type, meta.name);
        fallbackDiv.appendChild(iconSpan);
        const label = document.createElement('div');
        label.className = 'file-fallback-label';
        label.textContent = 'プレビューできないファイル形式です';
        fallbackDiv.appendChild(label);
        previewEl.appendChild(fallbackDiv);
      }

      // Always show file info bar below preview
      previewEl.appendChild(createFileInfoBar(meta));

      document.getElementById('downloadBtn').textContent = `📥 ${meta.name} をダウンロード`;
      attachmentView.style.display = 'block';
    } catch (attachErr) {
      console.error('Attachment decrypt error:', attachErr);
      attachmentView.style.display = 'none';
    }
  } else {
    attachmentView.style.display = 'none';
  }

  // Read status banner
  const banner = document.getElementById('readStatusBanner');
  banner.className = 'read-status-banner';
  if (data.burn_after_read) {
    banner.textContent = '✅ このメモは初めて開かれました（既読で消滅）';
    banner.classList.add('first-read');
    banner.style.display = 'flex';
  } else if (data.read_count === 1) {
    banner.textContent = '✅ このメモは初めて開かれました';
    banner.classList.add('first-read');
    banner.style.display = 'flex';
  } else if (data.read_count > 1 && data.max_reads > 0) {
    banner.textContent = '⚠️ このメモは' + data.read_count + '/' + data.max_reads + '回目の閲覧です';
    banner.classList.add('multi-read');
    banner.style.display = 'flex';
  } else if (data.read_count > 1) {
    banner.textContent = '⚠️ このメモは既に' + data.read_count + '回開かれています';
    banner.classList.add('multi-read');
    banner.style.display = 'flex';
  } else {
    banner.style.display = 'none';
  }

  // Meta info
  const created = new Date(data.created_at).toLocaleString('ja-JP');
  const expires = new Date(data.expires_at).toLocaleString('ja-JP');
  let metaHtml = `<span>🔐 E2E暗号化</span><span>📅 ${created}</span><span>⏱ ${expires}まで</span>`;
  if (data.max_reads > 0) metaHtml += `<span>📊 ${data.read_count}/${data.max_reads}回</span>`;
  if (data.password_protected) metaHtml += `<span>🔑 パスワード保護</span>`;
  document.getElementById('noteMeta').innerHTML = metaHtml;

  // Remove key from URL
  if (window.location.hash) {
    history.replaceState(null, '', window.location.pathname);
  }

  // Burn timer
  if (data.burn_after_read) {
    const expiresAt = new Date(data.expires_at);
    startDissolveSequence(expiresAt);
  }
}

// ---------------------------------------------------------------------------
// Dissolve sequence (smoke theme)
// ---------------------------------------------------------------------------
function formatRemaining(totalSeconds) {
  if (totalSeconds <= 0) return '消滅中...';
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) return `${h}時間${m}分${s}秒後に消滅`;
  if (m > 0) return `${m}分${s}秒後に消滅`;
  return `${s}秒後に消滅`;
}

function startDissolveSequence(expiresAt) {
  const now = Date.now();
  const totalSeconds = Math.max(0, Math.round((expiresAt.getTime() - now) / 1000));
  if (totalSeconds <= 0) { dissolveNote(); return; }

  const container = document.getElementById('burnTimerContainer');
  container.innerHTML = `
    <div class="burn-timer-bar">
      <div class="timer-label">
        💨 <span id="dissolveCountText">${formatRemaining(totalSeconds)}</span>
      </div>
      <div class="progress-track">
        <div class="progress-fill" id="progressFill" style="width:100%"></div>
      </div>
    </div>
  `;

  const progressFill = document.getElementById('progressFill');
  requestAnimationFrame(() => {
    progressFill.style.width = '0%';
    progressFill.style.transition = `width ${totalSeconds}s linear`;
  });

  const countText = document.getElementById('dissolveCountText');
  const startTime = Date.now();
  const timer = setInterval(() => {
    const elapsed = Math.round((Date.now() - startTime) / 1000);
    const remaining = totalSeconds - elapsed;
    if (remaining <= 0) {
      clearInterval(timer);
      dissolveNote();
    } else {
      countText.textContent = formatRemaining(remaining);
    }
  }, 1000);
}

function dissolveNote() {
  const noteContent = document.getElementById('noteText');
  noteContent.classList.add('dissolving');
  setTimeout(() => {
    document.getElementById('readNoteInner').style.display = 'none';
    document.getElementById('ashesState').style.display = 'flex';
  }, 3200);
}

// ---------------------------------------------------------------------------
// Toggle interaction: burn-after-read disables max reads
// ---------------------------------------------------------------------------
document.getElementById('burnToggle').addEventListener('change', function() {
  const maxReadsSelect = document.getElementById('maxReads');
  if (this.checked) {
    maxReadsSelect.disabled = true;
    maxReadsSelect.value = '0';
    maxReadsSelect.style.opacity = '0.4';
  } else {
    maxReadsSelect.disabled = false;
    maxReadsSelect.value = '1';
    maxReadsSelect.style.opacity = '1';
  }
});
// Initialize on load
document.getElementById('burnToggle').dispatchEvent(new Event('change'));

// ---------------------------------------------------------------------------
// File attachment: drag & drop / click to select
// ---------------------------------------------------------------------------
const MAX_FILE_SIZE = 3 * 1024 * 1024; // 3MB

function setupFileAttachment() {
  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  if (!dropZone || !fileInput) return;

  dropZone.addEventListener('click', (e) => {
    if (e.target.id === 'fileRemoveBtn' || e.target.closest('#fileRemoveBtn')) return;
    if (selectedFile) return;
    fileInput.click();
  });

  fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) handleFileSelect(e.target.files[0]);
  });

  dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-over'); });
  dropZone.addEventListener('dragleave', () => { dropZone.classList.remove('drag-over'); });
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    if (e.dataTransfer.files.length > 0) handleFileSelect(e.dataTransfer.files[0]);
  });
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function handleFileSelect(file) {
  document.getElementById('fileSizeError').style.display = 'none';
  if (file.size > MAX_FILE_SIZE) {
    document.getElementById('fileSizeError').style.display = 'block';
    return;
  }
  selectedFile = file;
  document.getElementById('dropZoneContent').style.display = 'none';
  document.getElementById('filePreview').style.display = 'block';
  document.getElementById('fileName').textContent = file.name;
  document.getElementById('fileSize').textContent = formatFileSize(file.size);

  const thumb = document.getElementById('fileThumb');
  if (file.type.startsWith('image/')) {
    thumb.src = URL.createObjectURL(file);
    thumb.style.display = 'block';
  } else {
    thumb.style.display = 'none';
  }
}

function removeAttachment(e) {
  if (e) { e.stopPropagation(); e.preventDefault(); }
  selectedFile = null;
  document.getElementById('fileInput').value = '';
  document.getElementById('filePreview').style.display = 'none';
  document.getElementById('dropZoneContent').style.display = 'block';
  document.getElementById('fileSizeError').style.display = 'none';
  const thumb = document.getElementById('fileThumb');
  if (thumb.src) { URL.revokeObjectURL(thumb.src); thumb.src = ''; }
}

// ---------------------------------------------------------------------------
// Attachment preview helpers
// ---------------------------------------------------------------------------
function getFileTypeIcon(mimeType, fileName) {
  if (!mimeType) return '📄';
  if (mimeType.startsWith('image/')) return '🖼️';
  if (mimeType.startsWith('video/')) return '🎬';
  if (mimeType.startsWith('audio/')) return '🎵';
  if (mimeType === 'application/pdf') return '📑';
  if (mimeType.includes('zip') || mimeType.includes('compress') || mimeType.includes('archive')) return '🗜️';
  if (mimeType.includes('spreadsheet') || mimeType.includes('excel') || (fileName && /\.xlsx?$/i.test(fileName))) return '📊';
  if (mimeType.includes('presentation') || mimeType.includes('powerpoint') || (fileName && /\.pptx?$/i.test(fileName))) return '📽️';
  if (mimeType.includes('document') || mimeType.includes('word') || mimeType.includes('text') || (fileName && /\.docx?$/i.test(fileName))) return '📝';
  if (mimeType.includes('json') || mimeType.includes('xml') || mimeType.includes('javascript') || mimeType.includes('html')) return '💻';
  return '📄';
}

function createFileInfoBar(meta) {
  const bar = document.createElement('div');
  bar.className = 'attachment-info-bar';
  bar.innerHTML = `
    <span class="attachment-info-icon">${getFileTypeIcon(meta.type, meta.name)}</span>
    <span class="attachment-info-name">${escapeHtml(meta.name)}</span>
    <span class="attachment-info-detail">${formatFileSize(meta.size)}${meta.type ? ' ・ ' + escapeHtml(meta.type) : ''}</span>
  `;
  return bar;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Download decrypted attachment
// ---------------------------------------------------------------------------
function downloadAttachment() {
  if (!decryptedAttachment) return;
  const url = URL.createObjectURL(decryptedAttachment.blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = decryptedAttachment.name || 'attachment';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Bind event listeners (CSP-safe, no inline onclick)
// ---------------------------------------------------------------------------
document.getElementById('createBtn').addEventListener('click', createNote);
document.getElementById('copyBtn').addEventListener('click', copyUrl);
document.getElementById('fileRemoveBtn').addEventListener('click', function(e) { removeAttachment(e); });
document.getElementById('downloadBtn').addEventListener('click', downloadAttachment);
document.getElementById('submitPasswordBtn').addEventListener('click', submitPassword);

setupFileAttachment();
window.addEventListener('popstate', init);
init();
