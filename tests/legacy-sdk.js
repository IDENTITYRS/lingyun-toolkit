/* 仅用于预览测试：复现旧 SDK 的 void sendAction + 无请求 ID 回调协议。 */
(function () {
  'use strict';
  const handlers = {};
  const ready = [];
  let params = null;
  const post = payload => window.parent.postMessage({__typetale_plugin: true, ...payload}, '*');
  window.addEventListener('message', event => {
    if (event.source !== window.parent) return;
    const message = event.data || {};
    if (message.__typetale_plugin !== true) return;
    if (message.type === 'init_params') {
      params = message.params || {};
      ready.splice(0).forEach(callback => callback(params));
    }
    if (message.type === 'action') {
      (handlers[message.action] || []).forEach(callback => callback(message.data));
    }
  });
  window.PluginSDK = {
    sendAction(action, data) { post({type: 'plugin_action', action, data}); },
    onAction(action, callback) { (handlers[action] || (handlers[action] = [])).push(callback); },
    onReady(callback) { if (params) callback(params); else ready.push(callback); },
    saveParam(key, value) { post({type: 'save_param', key, value}); }
  };
  post({type: 'plugin_ready'});
})();
