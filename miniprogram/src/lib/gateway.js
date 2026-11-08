const config = require('../config');
function request(path, method, data, session) {
  if (!config.integrationReady) return Promise.reject(Error('真实账号接入尚未配置，请先使用本地预览。'));
  return new Promise((resolve, reject) => wx.request({
    url: config.apiBase + path, method, data, timeout: 60000,
    header: Object.assign({ 'Content-Type': 'application/json' }, session ? { Authorization: 'Bearer ' + session } : {}),
    success: response => {
      if (response.statusCode < 200 || response.statusCode >= 300) {
        const error = Error(response.data && typeof response.data.error === 'string' ? response.data.error : '操作未完成，请稍后重试。');
        error.status = response.statusCode;
        return reject(error);
      }
      resolve(response.data);
    }, fail: () => reject(Error(path === '/phone/apply' ? '连接中断，开通结果尚未确认。请查看开通进度，暂时不要重复申请。' : method === 'PUT' ? '连接中断，保存结果尚未确认。草稿已保留，请读取最新设置后核对。' : '连接失败，请检查网络后重试。'))
  }));
}
module.exports = { request };
