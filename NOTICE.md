
# 来源与开发说明

本项目是使用 Codex 辅助开发的个人实践作品，围绕官方微信 iLink 与本机 Codex 的连接，补充 Windows 会话管理界面和使用流程。

## 上游来源

`src/bridge_runtime.py` 基于 [BigPizzaV3/CodexPlusPlus](https://github.com/BigPizzaV3/CodexPlusPlus) 中的 [tools/codex-wechat/codex_wechat.py](https://github.com/BigPizzaV3/CodexPlusPlus/blob/main/tools/codex-wechat/codex_wechat.py) 修改。其原有 iLink 轮询、登录和 Codex 后端骨架不作为本项目从零独立创作的内容。上游权利归原作者所有。

上游使用 GNU AGPL v3。本公开项目沿用 GNU AGPL v3，完整文本见根目录 `LICENSE`。公开源码时保留此说明和许可；分发基于本项目构建的程序时，应同时提供相应版本的完整源代码、构建说明和许可。

## 本项目中的改造

- 新增 Tkinter Windows 管理界面与后台服务层，浏览、搜索、预览和接管已有 Codex 会话。
- 统一使用一个常驻 app-server，接管成功后才更新默认会话；冲突时保留配置，明确提示占用。
- 补充持久会话创建、配置备份、取消接管及重复启动保护。
- 在本地桥接基础上补充附件收发、桌面截图与连接超时处理。
- 统一界面用语，整理隔离回归测试、验证记录和公开展示材料。

项目主导者负责提出需求、明确使用场景、迭代交互并反馈真实使用结果；代码实现、整理和部分验证使用 AI 辅助。文件传输与故障场景的结论以验证记录为准。

## 外部组件

OpenAI Codex、微信 iLink、Python、Tkinter、Pillow、cryptography 与 PyInstaller 均属于各自的项目或服务。它们没有被作为本项目原创代码发布。此文件夹不包含 Codex 程序或 Python 运行时，也不包含预编译 EXE。使用与再分发这些组件时，适用其各自许可和服务要求。

本项目不是 OpenAI 或腾讯官方产品，使用官方聊天通道不意味着获得官方背书。
