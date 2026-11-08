# 模块与数据边界

| 路径 | 内容 |
| --- | --- |
| production/portal/code | 官网、账号、开通图解、帮助、后台网页、HTTP 网关 |
| production/admin/code | 管理操作、模板角色卡、余额与流水、充值、运营费用、生命周期 |
| production/onboarding | 申请、目录与部门分配、微信连接、短信适配、企业路由 |
| production/app-pool | 应用池、热备与恢复控制 |
| production/*.py | 原始应用运行时、人格/记忆/语音/图像/提醒、调用计量 |
| production/enterprises | 企业隔离后的控制面及应用运行时副本 |
| production/shared-runtime-* | 共享运行时与按需生命周期控制 |
| production/releases | 发布模板源码，保留未来创建应用所需结构 |
| production/deployment/systemd | 原服务与定时任务的结构样本 |
| miniprogram | 已暂停的原生设置小程序源码及合成测试 |

Web 门户通过受限 Unix Socket 与管理员、开通和应用桥接通信；门户本身不挂载应用 Secret 或聊天数据库。真实身份、应用归属、钱包与支付流水在各自服务内维护。副本之间可能存在历史差异，归档保留原布局。

本仓库是源码归档，不含运行状态与真实业务数据。多个运行时副本是原项目结构，不意味着有同等数量的独立在线应用。所有配置、状态与第三方权限均需在新环境中重新建立。
