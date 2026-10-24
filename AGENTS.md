# AGENTS.md

给 AI agent 的操作说明。用户只需要说一个行业名（如「跨境电商」「宠物用品」），
你就执行下面**一条命令**，不要自己拆解步骤、不要改代码。

```powershell
python run.py <行业名>
```

`run.py` 会自动切到项目自带的 `.venv`，并调用 `run_daily.py --industry <行业名>`。
完整流程：**启动 MuMu 模拟器 → 打开小红书 → 按行业链路逐环节搜索 → 三层去重 →
环节归属校验 → 大模型三轴分析 → 生成 HTML/CSV/MD → 上传 IMA 知识库**。

跑一次约 **20–60 分钟**（取决于行业规模和广告密度）。用 `run_in_background` 跑，
然后定期读输出即可，不要中途打断。

---

## 常用变体

| 需求 | 命令 |
| --- | --- |
| 正式跑（自动传 IMA 知识库） | `python run.py 宠物用品` |
| 先抓少量试试 | `python run.py 宠物用品 --per-keyword 2` |
| 自检环境 | `python run.py --check` |
| 看已有哪些行业 | `python run.py --list` |

## 前置条件（缺任何一项都会在自检里报出来）

- MuMu 模拟器已安装（脚本自动定位，不需要先手动打开）
- adb 设备名**不固定**（`emulator-5554` 或 `127.0.0.1:16384` 都可能），脚本会自动枚举并挑出装了小红书那台，
  所以不要在任何地方写死 serial
- 小红书 App 已登录（掉登录时返回码 `8`，需要用户在模拟器里扫码一次）
- `config.yaml` 里已填：大模型 key、IMA 凭证与 `ima.knowledge_base_id`

## 退出码

| 码 | 含义 | 怎么办 |
| --- | --- | --- |
| 0 | 成功 | 读最后的汇总行 |
| 3 | 小红书未登录 | 让用户跑 `python -m core.emulator` 扫码 |
| 4 | 没抓到广告 | 换行业名，或看是否被限流 |
| 7 | 连不上模拟器 | 跑 `python -m core.emulator` 排查 |
| 8 | 需要重新扫码登录 | 让用户扫码后重跑 |
| 9 | 模拟器没能启动 | 检查 MuMu 安装，或 `emulator.auto_start: false` 后手动开 |

## 产物在哪

```
data/reports/小红书广告日报_<日期>.html    折叠式报告（人看）
data/reports/小红书广告日报_<日期>.md      文本摘要（已上传 IMA）
data/reports/小红书广告明细_<日期>.csv     明细表（已上传 IMA）
```

跑完向用户汇报：**采到多少条、覆盖几个环节、哪些环节零投放、群和知识库是否发送成功**。

## 不要做

- 不要用 `--source web`：网页版不渲染「广告」标签，抓不到广告，只有 App 端有
- 不要删 `data/notes.db`：去重和品牌别名靠它，删了会大量重复采集
- 不要改 `config.yaml` 里的 `industry.search_keywords` 之类的旧字段来"换行业"，换行业只用命令参数
- 不要为了"快一点"关掉去重（`max_per_advertiser` / `max_per_author`）：Ozon 这类一次投放会写成十几篇笔记

## 想改行业拆解

行业链路和各环节搜索词缓存在 `data/industry_plan_<行业>.json`，可以直接编辑后重跑；
删掉该文件则会重新让模型拆解。
