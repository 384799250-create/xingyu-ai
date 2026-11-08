# 原生设置小程序（已暂停）

该目录保留微信原生设置小程序源码、手动保存逻辑与六组本地测试。小程序项目于 2026-11-05 暂停推进；网页设置继续使用，URL 直达和手机号入口默认关闭。

使用微信开发者工具导入此目录，把 project.config.json 中的示例 AppID 换成自己的 AppID；在 src/config.js 中配置自己的 HTTPS API 域名。AppSecret 只能配置到服务端。

本地测试：`node --test tests/*.test.js`。这些测试使用合成数据，不发送微信消息，不证明平台审核、备案或真实手机登录已经完成。归档中保留的接口契约见 SERVER-CONTRACT.md。
