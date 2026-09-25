(function (root) {
  'use strict';
  root.createLingyunUpdatePanel = function (request, isBusy, onInstallState) {
    const el = id => document.getElementById(id);
    let snapshot = {}, requesting = false, started = false, timer = null;
    let history = [], selected = null;
    function historyView() {
      const index = Math.max(0, history.findIndex(item => item.version === selected));
      const item = history[index];
      selected = item ? item.version : null;
      el('updateHistoryPosition').textContent = history.length ? `${index + 1} / ${history.length}` : '0 / 0';
      el('updateHistoryTitle').textContent = item ? `V${item.version} 更新内容` : '暂无历史记录';
      el('updateHistoryNotes').textContent = item ? item.notes : '';
      el('updateHistoryPrev').disabled = index <= 0;
      el('updateHistoryNext').disabled = index >= history.length - 1;
    }
    function controls() {
      el('checkUpdate').disabled = requesting || !!snapshot.running || isBusy();
      el('installUpdate').disabled = requesting || !!snapshot.running || isBusy();
      el('checkUpdate').textContent = snapshot.state === 'checking' ? '正在检查…' : '检查更新';
      historyView();
    }
    function render(data) {
      snapshot = data;
      const available = data.state === 'available';
      el('updateVersion').textContent = '当前版本：V' + (data.current_version || '3.0.0');
      el('updateHint').textContent = available ? '发现新版本 V' + data.latest_version : '';
      el('updateMessage').textContent = data.message || '点击检查更新。';
      el('updateMessage').className = 'update-message ' + (data.ok === false ? 'err' : '');
      el('installUpdate').hidden = !available;
      el('installUpdate').textContent = '更新到 V' + (data.latest_version || '');
      const releases = Array.isArray(data.releases) ? data.releases : [];
      const compare = (a, b) => {
        const x = a.split('.').map(Number), y = b.split('.').map(Number);
        return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
      };
      history = releases.filter(item => compare(item.version, data.current_version || '3.0.0') <= 0);
      const upcoming = releases.filter(item => compare(item.version, data.current_version || '3.0.0') > 0);
      el('updateNewNotes').hidden = !upcoming.length;
      el('updateNewNotes').textContent = upcoming.map(item => `V${item.version} 更新内容\n${item.notes}`).join('\n\n');
      const progress = el('updateProgress');
      progress.hidden = !data.running && data.state !== 'done';
      if (typeof data.progress === 'number') progress.value = data.progress;
      else progress.removeAttribute('value');
      el('updateBytes').textContent = data.total_bytes ? `${Math.round((data.downloaded_bytes || 0) / 1024)} / ${Math.round(data.total_bytes / 1024)} KB` : '';
      onInstallState(!!data.running && data.state !== 'checking');
      controls();
    }
    async function call(action, payload) {
      if (requesting) return;
      clearTimeout(timer);
      requesting = true; controls();
      try {
        const result = await request(action, payload || {}, {timeoutMs: 30000});
        if (result.ok === false) throw new Error(result.error || '读取更新状态失败。');
        render(result.data || result);
      } catch (error) {
        el('updateMessage').textContent = (error.message || String(error)) + (snapshot.running ? ' 正在重新读取进度…' : ' 可点击检查更新重试。');
        el('updateMessage').className = 'update-message err';
      } finally {
        requesting = false; controls();
        if (snapshot.running) timer = setTimeout(() => call('update_status'), 1500);
      }
    }
    el('checkUpdate').addEventListener('click', () => {
      if (!isBusy()) call('check_update');
    });
    el('installUpdate').addEventListener('click', () => {
      if (!isBusy()) call('install_update');
    });
    [['updateHistoryPrev', -1], ['updateHistoryNext', 1]].forEach(([id, direction]) => {
      el(id).addEventListener('click', () => {
        const next = history.findIndex(item => item.version === selected) + direction;
        if (history[next]) selected = history[next].version;
        historyView();
      });
    });
    return {controls, start() {
      if (started) return;
      started = true;
      call('check_update', {automatic: true});
    }};
  };
})(window);
