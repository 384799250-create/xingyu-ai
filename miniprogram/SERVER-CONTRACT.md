# 小程序设置网关 · 已部署接口

前缀 https://www.example.com/ai/mini/v1，版本 mini-binding-20261104.1。已支持微信 code 交换与首次应用归属验证，以及原设置链接连接；该小程序 AppSecret 已配置且微信官方凭据验证成功，自动登录仍需真机验证。OpenID 不视为企业成员或支付身份。

- GET /status：公开版本、服务就绪和 wechatReady（只表示服务器已配置凭据，不代表真机交换成功）。
- POST /wechat/session：仅接受 wx.login 的 code。已绑定时签发原应用会话；首次返回十分钟单次 ticket，不返回 OpenID/session_key。
- POST /wechat/bind/account：ticket、username、password，核验已有账号并连接其应用。已 ready 的开通结果可恢复原 subject，未完成记录不能猜测归属。
- POST /link/session：完整 link 与可选 ticket。mini-only bridge 验证该应用管理员/owner 的有效原设置 token；有 ticket 才绑定该微信，没有时只签发当前设备七天会话。支持原 /xiaoyi/ 管理员和既有已分配应用。
- POST /wechat/unbind：现有 Bearer 会话，解除当前微信映射并撤销该映射全部 mini 会话，不删除应用或业务资料。
- POST /session：JSON username/password，复用 Portal 原账号密码和同一组登录限流；已有关联应用才签发七天不透明会话。密码、AppSecret、原网页 token、panel cookie 不返回客户端。
- POST /logout：Authorization Bearer 会话，撤销本会话。
- GET /applications：只返回该会话原账号已授权应用，id 固定为 current。
- GET /applications/current/settings：人格分项 DTO 与原后端 revision。scope 是不含个人资料的账号隔离摘要，只用于客户端避免另一账号的草稿混入，不可作为授权依据。
- GET /applications/current/templates：已发布官方模板、实际版本、语音和参考图标记。
- POST /applications/current/templates/prepare：id/version，返回 profile、模板来源和请求号。只准备草稿，不切换人格或保存设置。
- PUT /applications/current/settings：revision、selected、edits、templateDrafts。只允许人格分项 patch；服务端读取原资料后合并，保留未展示字段，通过原设置 API 保存。模板重新核验版本和摘要，复用原语音、参考图、名称头像同步和回执。冲突 409，未登录 401，无权限 403。网络结果不明不重试写入，保留草稿并读取核对。

原账号认证会话保存在独立 mini_sessions 表（只存 token 摘要），绑定原账号密码摘要和 subject；改密、注销、账号归属变化或过期立即拒绝。不接收可信客户端成员 ID、企业、应用 URL 或其他应用标识。每次调用原 subject bridge 重新核验分配；仅支持 current，不能更改 URL 访问别人的应用。

已有 panel 凭据仅在服务端按 bridge 生成的固定 HTTPS 域名、严格应用路径使用；不跟随重定向，不下发给客户端。未开放充值、财务写入、消息发送、模型调用或任意 HTTP 代理接口。

支付、余额查询和完整设置页面为后续迁移阶段。保持原钱包、订单、归属与余额；支付不能复用公众号 OpenID 或旧网页支付参数。

微信会话与映射分别位于 mini_identity_sessions、mini_bindings，票据位于 mini_binding_tickets。wx.login 只访问固定微信端点，禁止重定向；原始 OpenID 仅交换时用于计算 AppID 作用域摘要，不持久化。绑定唯一性和单次票据事务保护；客户端仅使用 current。最初管理员的 mini subject 单独命名，未改变网站 bridge 对原应用的限制。AppSecret 只允许服务端 /data/miniprogram-credentials.json，owner 当前服务用户、0600，前端无密钥。
