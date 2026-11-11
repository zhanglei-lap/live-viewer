# P0 验收记录

日期：2026-10-10。交付为 Qt / PySide6 原生桌面原型与设计接口规范。所有媒体、账号和网络状态为模拟；未接入正式取流、登录或 PotPlayer 控制。

## 交付

- Windows 便携包：dist/p0/LiveViewer-P0-win-x64.zip。
- 本机执行入口：dist/p0/LiveViewer-P0/LiveViewer-P0.exe。
- 原型源码：prototypes/p0/app.py、model.py、theme.qss、validate.py。
- 视觉、页面、播放状态、通知规则与控制探针：docs/p0/design-spec.md。
- 事件结构 v1：docs/p0/event.schema.json。

## 验证结果

| 检查 | 结果 |
|---|---|
| 源码原型 29 项交互检查 | 通过 |
| 本机 125% 缩放与最低 920×620 窗口 | 通过 |
| 150% / 200% 缩放交互检查与截图 | 通过 |
| 打包后独立 EXE 29 项交互检查 | 通过 |
| 包内源码、主题和事件结构一致性 | 通过 |
| 包内许可证与来源信息 | 已包含 |
| 个人设置与登录数据 | 未包含 |

场景覆盖未开播、TLS 最终失败、同类故障去重、告示跨页、登录成功不覆盖播放错误、用户停止后迟到的成功 / 失败、恢复成功与尝试用尽、强提醒偏好、关闭自动恢复、日志等级 / 文本筛选和事件详情。

图片已经人工查看，确认固定告示、状态字号、图标与窄窗口排版。后台 / 全屏真实通知和 PotPlayer 接管仍在后续阶段验收。

## 打包修正

首次构建从桌面工具继承的 PATH 中误收集了不兼容的同名 ICU 库，导致 QtCore 导入失败。已确认源码运行使用 Windows 的系统 ICU，并把原型构建的依赖搜索路径隔离为系统、Python 和本原型的 Qt 环境。最终构建重新生成，独立运行检查通过；不依赖人工移动 DLL。

验证报告：.research/p0-validation.json、p0-dpi-summary.json、p0-packaged-validation.json、p0-package-artifacts.json。最终包的摘要以 p0-package-artifacts.json 为准。

P0 完成。P1 按本规范接入正式事件日志、播放状态及业务流程；P0 的模拟模型不作为已经完成生产日志的证据。
