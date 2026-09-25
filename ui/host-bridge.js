/* 新 SDK 返回 Promise；旧 SDK 通过 onAction 回传。每次请求只发送一次。 */
(function (root) {
  'use strict';

  root.createLingyunHostBridge = function (sdk) {
    const pending = new Map();
    const listening = new Set();
    const expired = new Set();

    function listen(action) {
      if (listening.has(action) || typeof sdk.onAction !== 'function') return;
      sdk.onAction(action, function (result) {
        // 旧协议没有请求 ID。超时后的迟到结果只能丢弃，不能交给下一次请求。
        if (expired.delete(action)) return;
        const request = pending.get(action);
        if (!request || request.mode === 'promise') return;
        if (request.mode === 'sending') {
          request.earlyResult = result;
          request.hasEarlyResult = true;
        } else {
          request.finish(null, result);
        }
      });
      listening.add(action);
    }

    return function requestAction(action, data, options) {
      if (!sdk || typeof sdk.sendAction !== 'function') {
        return Promise.reject(new Error('未连接字字动画宿主，请在作品插件面板中打开。'));
      }
      if (pending.has(action)) {
        return Promise.reject(new Error('该操作正在等待宿主返回，请勿重复提交。'));
      }
      if (expired.has(action)) {
        return Promise.reject(new Error('上次操作超时且尚未返回，请确认执行结果后重新打开插件。'));
      }
      return new Promise(function (resolve, reject) {
        const request = {mode: 'sending', timer: null, hasEarlyResult: false};
        request.finish = function (error, result) {
          if (pending.get(action) !== request) return;
          pending.delete(action);
          clearTimeout(request.timer);
          if (error) reject(error);
          else if (!result || typeof result !== 'object' || Array.isArray(result)) {
            reject(new Error('宿主返回的插件结果格式无效。'));
          } else resolve(result);
        };
        pending.set(action, request);
        const rawTimeout = Number(options && options.timeoutMs);
        const timeoutMs = Number.isFinite(rawTimeout) && rawTimeout > 0 ? rawTimeout : 600000;
        request.timer = setTimeout(function () {
          if (request.mode === 'callback') expired.add(action);
          request.finish(new Error('等待宿主返回超时（' + action + '）。请确认字字动画后端正常运行；操作可能仍在执行，请检查结果后再重试。'));
        }, timeoutMs);
        try {
          listen(action);
          const result = sdk.sendAction(action, data || {}, options);
          if (result && typeof result.then === 'function') {
            // 新 SDK 以带请求 ID 的 Promise 为准，忽略同时广播的同名回调。
            request.mode = 'promise';
            Promise.resolve(result).then(
              value => request.finish(null, value),
              error => request.finish(error || new Error('宿主请求失败。'))
            );
          } else {
            request.mode = 'callback';
            if (request.hasEarlyResult) request.finish(null, request.earlyResult);
            else if (typeof sdk.onAction !== 'function') {
              request.finish(new Error('当前字字动画版本缺少插件结果回调接口，请升级后重试。'));
            }
          }
        } catch (error) {
          request.finish(error);
        }
      });
    };
  };
})(window);
