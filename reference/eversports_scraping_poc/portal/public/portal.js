/* Eversync Portal — client-side logic */
(function () {
  'use strict';

  var selectedFile = null;

  /* ── inline-style alerts (no CSS class dependency) ─── */
  function showErr(msg) {
    console.error('[eversync] error:', msg);
    var d = document.getElementById('errMsg');
    d.innerHTML = msg;
    d.style.cssText =
      'display:block;background:#2a1515;border:1px solid #7f1d1d;' +
      'color:#fca5a5;padding:12px 15px;border-radius:6px;' +
      'margin-bottom:14px;font-size:14px;line-height:1.5;';
    d.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function showWarn(msg) {
    console.warn('[eversync] warn:', msg);
    var d = document.getElementById('warnMsg');
    d.innerHTML = msg;
    d.style.cssText =
      'display:block;background:#2a1f0a;border:1px solid #78350f;' +
      'color:#fcd34d;padding:12px 15px;border-radius:6px;' +
      'margin-bottom:14px;font-size:14px;line-height:1.5;';
    d.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function clearAlerts() {
    ['errMsg', 'warnMsg'].forEach(function (id) {
      var d = document.getElementById(id);
      d.style.display = 'none';
      d.innerHTML = '';
    });
  }

  /* ── file drop zone ─────────────────────────────────── */
  var dz = document.getElementById('dropZone');

  dz.addEventListener('dragover', function (e) {
    e.preventDefault();
    dz.classList.add('over');
  });
  dz.addEventListener('dragleave', function () {
    dz.classList.remove('over');
  });
  dz.addEventListener('drop', function (e) {
    e.preventDefault();
    dz.classList.remove('over');
    if (e.dataTransfer.files[0]) pickFile(e.dataTransfer.files[0]);
  });

  document.getElementById('fileInput').addEventListener('change', function () {
    console.log('[eversync] file selected:', this.files[0] && this.files[0].name);
    if (this.files[0]) pickFile(this.files[0]);
  });

  function pickFile(f) {
    console.log('[eversync] pickFile:', f.name, f.type);
    /* accept any file named *.json regardless of MIME type (OS can vary) */
    if (!f.name.endsWith('.json') && f.type.indexOf('json') === -1) {
      showErr('Only .json files are accepted (got: ' + (f.type || 'unknown type') + ').');
      return;
    }
    selectedFile = f;
    var fn = document.getElementById('fileName');
    fn.textContent = '✓  ' + f.name;
    fn.style.display = 'block';
    clearAlerts();
    console.log('[eversync] selectedFile set OK');
  }

  /* ── submit ─────────────────────────────────────────── */
  document.getElementById('btnUpload').addEventListener('click', function () {
    console.log('[eversync] Upload clicked');

    var lid       = document.getElementById('locationId').value.trim();
    var sec       = document.getElementById('secret').value.trim();
    var pasteText = document.getElementById('cookieText').value.trim();

    console.log(
      '[eversync] lid:', lid ? '(set)' : '(EMPTY)',
      '| sec:', sec ? '(set)' : '(EMPTY)',
      '| pasteText:', pasteText ? '(set)' : '(empty)',
      '| selectedFile:', selectedFile ? selectedFile.name : '(none)'
    );

    if (!lid) { showErr('Please enter your Location ID.'); return; }
    if (!sec) { showErr('Please enter your Portal Password.'); return; }

    var cookieBlob;
    if (pasteText) {
      try {
        var parsed = JSON.parse(pasteText);
        if (!Array.isArray(parsed) || parsed.length === 0)
          throw new Error('must be a non-empty array');
        cookieBlob = new File([pasteText], 'cookies.json', { type: 'application/json' });
        console.log('[eversync] using pasted JSON,', parsed.length, 'cookies');
      } catch (e) {
        showErr(
          'Pasted text is not valid cookie JSON: ' + e.message +
          '<br>It must start with <code>[{</code> and end with <code>}]</code>.'
        );
        return;
      }
    } else if (selectedFile) {
      cookieBlob = selectedFile;
      console.log('[eversync] using file:', selectedFile.name);
    } else {
      showErr('Please paste your cookie JSON above, or select a .json file.');
      return;
    }

    /* loading state */
    clearAlerts();
    var btn = document.getElementById('btnUpload');
    document.getElementById('btnLabel').textContent  = 'Uploading…';
    document.getElementById('spinner').style.display = 'inline-block';
    btn.disabled = true;
    console.log('[eversync] sending to /upload ...');

    var controller   = new AbortController();
    var timeoutTimer = setTimeout(function () { controller.abort(); }, 15000);

    var fd = new FormData();
    fd.append('locationId', lid);
    fd.append('secret',     sec);
    fd.append('cookies',    cookieBlob);

    fetch('/upload', { method: 'POST', body: fd, signal: controller.signal })
      .then(function (res) {
        console.log('[eversync] response status:', res.status);
        return res.json().then(function (data) { return { s: res.status, d: data }; });
      })
      .then(function (r) {
        console.log('[eversync] response body:', JSON.stringify(r.d));
        if (r.s === 429) { showWarn('Too many attempts — please wait 15 minutes.'); return; }
        if (r.s === 401) { showErr('Invalid credentials — check your Location ID and Portal Password.'); return; }
        if (r.s === 400) { showErr((r.d && r.d.error) || 'Invalid cookie file.'); return; }
        if (r.s !== 200) { showErr('Upload failed (' + r.s + '): ' + ((r.d && r.d.error) || 'Unknown error.')); return; }

        /* success */
        var expiry = r.d.expiresAt
          ? new Date(r.d.expiresAt).toLocaleDateString(undefined,
              { year: 'numeric', month: 'long', day: 'numeric' })
          : 'unknown';
        document.getElementById('expiryBadge').textContent = 'Session valid until ' + expiry;
        document.getElementById('mainForm').style.display   = 'none';
        document.getElementById('successBox').style.display = 'block';
        window.scrollTo({ top: 0, behavior: 'smooth' });
      })
      .catch(function (err) {
        console.error('[eversync] fetch error:', err.name, err.message);
        if (err.name === 'AbortError') {
          showErr(
            'Request timed out after 15 s.<br>' +
            'Is the portal server running? Run: <code>node portal/src/server.js</code>'
          );
        } else {
          showErr(
            'Cannot reach the server (' + err.message + ').<br>' +
            'Make sure you are on <code>http://localhost:3000</code> and the server is running.'
          );
        }
      })
      .finally(function () {
        clearTimeout(timeoutTimer);
        document.getElementById('btnLabel').textContent  = 'Upload & Activate';
        document.getElementById('spinner').style.display = 'none';
        btn.disabled = false;
      });
  });

  console.log('[eversync] portal.js loaded OK');
}());
