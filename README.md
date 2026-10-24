# 小红书广告洞察工具

自动抓取小红书上带广告/商业标识的笔记，用大模型拆解 **特殊人群 / 特殊场景 / 特殊需求**，
每天生成一份日报并自动导入 IMA 知识库。

---

## 一、它能产出什么

| 产出 | 位置 | 说明 |
|---|---|---|
| IMA 知识库 | 「搞薯条看广告」 | Markdown 摘要 + CSV 明细，可直接问答检索 |
| HTML 日报 | `data/reports/小红书广告日报_日期.html` | 可视化分布图 + 精选 + 全量表格 |
| CSV 明细 | `data/reports/小红书广告明细_日期.csv` | 可直接导入多维表格/Excel 做二次分析 |
| 数据库 | `data/notes.db` | 历史笔记去重 + 分析结果缓存 |

> 报告长什么样：先跑 `python run_daily.py --demo` 立刻生成一份示例日报，不用联网也不用 key。

---

## 二、为什么必须用 Android 模式

**小红书网页版不渲染「广告」标签。** 实测桌面端首页与搜索结果页的卡片只有
封面 / 标题 / 作者 / 时间 / 点赞，全页 HTML 里「赞助」出现 0 次；移动端 H5 则直接
返回 404 或「打开 App」下载引导页。

**「广告」标签只有 App 里有。** 因此本工具默认走 Android 模式：通过 uiautomator2
读取模拟器/真机的 UI 树（不破解 SSL、不抓包），「广告」是一个独立 TextView，
其坐标落在哪张卡片的 bounds 内，那张卡片就是广告。

---

## 三、行业链路模式（核心玩法）

给它一个行业名，它会先把行业拆成商业链路上的各个环节，再**逐环节定向采样**，
每个关键词抓 5 条，总数不限 —— 目标是「把这个行业的每个部分都覆盖到」，
而不是凑够某个条数。

```powershell
.venv/Scripts/python.exe run_daily.py --source android --industry 跨境电商
```

拆解结果缓存在 `data/industry_plan_<行业>.json`，**可以直接编辑**：改环节名、加减搜索词，
再跑就会按你的版本执行，不用重新调模型。

以「跨境电商」为例，拆出 12 个环节：

```
开店入驻 / 选品调研 / 货源供应链 / 建站工具 / 物流货代 / 收款结汇
运营工具 / 广告投流 / 内容获客 / 合规税务 / 培训陪跑 / 售后履约
```

实测结果：12 个环节里 **9 个有投放**，物流货代、收款结汇、运营工具三个环节
在小红书**完全没有广告**（换了 2~3 个搜索词都没命中「广告」标识）。
报告里会把这三个环节也列出来并标注「该环节未采集到广告投放」——
**哪个环节在小红书有投放、哪个没有，本身就是投放地图的一部分**。

报告是**折叠式**的：每个行业环节是一个可展开的大类（原生 `<details>`，无需 JS 也能用），
折叠时只显示「序号 + 环节名 + 一句话说明 + 条数」，
点开才展开该环节的广告卡片与三轴拆解；右上角有「全部展开 / 全部收起」。
零投放的环节也会列出，标「未投放」并展开说明。

**每个环节的词扩充到 6 个**（`core/industry.py` 的 `expand_keywords`，可反复调用继续加词）。
词一多会带来副作用：泛词会把别的行业广告带进来，比如「跨境小白训练营」搜出留学、MBA、求职培训。
所以分析后会再加一道 **LLM 环节复核**（`check_segments`）：逐条判断「投放方是不是在为这个环节提供
产品/服务」，只看广告主在卖什么，不看标题里有没有关键词。实测剔掉了 60 条跨行业广告
（UCL 留学、中欧 MBA、留学申诉、定制家具、奶粉、威士忌），剩下的才是真正的行业投放。

---

## 四、去重与稳定性机制

**⓪ 同一广告只留一条（广告主维度）**
同一次投放常被写成很多篇不同的笔记 —— 实测「跨境电商」41 条里，Ozon 一家占了 **14 篇**，
小红书电商占 8 篇，实际只有 17 个不同的广告。所以去重不止「同一篇笔记」，
还做到「同一广告主」：

- **采集时**：候选卡片标题里就带已采品牌词 → 直接跳过，连详情都不进（省 8 秒/条）；
  标题不带但正文带 → 进详情后丢弃。
- **分析后**：按 `advertiser` 归并（`industry.max_per_advertiser`，默认 1），
  再按**作者账号**归并（`industry.max_per_author`，默认 1）——
  一个号连发多条比品牌词更硬：实测 Ozon 那 14 篇里，有 13 篇是同一个
  账号「Ozon Global」发的。

配额也随之改成 **每个关键词目标 5 条**（`industry.per_keyword`），
翻页遇到重复直接跳过，实在凑不够就少点 —— 不硬凑。

**① 重复广告永不重采**
去重做三层：`note_id`（标题+作者指纹）→ 标题指纹（前 16 字归一化）→ **分享短链**
（`xhslink.cn/xxx`，同一条笔记的短链恒定，能拦住「同一篇笔记从信息流和搜索两个入口进来」的情况）。
历史库全量参与比对，一旦采过，以后搜到直接跳过。
代价是短链判断发生在进了详情页之后，会浪费一次约 8 秒的详情访问。

**② App 被系统杀掉会自动重开**
每屏采集前检查前台包名，发现不是 `com.xingin.xhs` 就自动 `app_start` 重开、
等待加载、**并重新进入当前搜索词**继续采集。模拟器内存紧张时小红书经常被后台清理，
没有这个机制整轮会直接跑空。

```powershell
# 抓 100 条广告笔记（模拟器需已启动、已登录小红书）
.venv/Scripts/python.exe run_daily.py --source android --limit 100
```

采集方式在 `config.yaml` 的 `android.mode` 里切换：

| 模式 | 行为 | 适用 |
|---|---|---|
| `search`（默认） | 按 `android.search_keywords` 逐个搜索，抓搜索结果里的广告 | 按行业铺量，覆盖面可控，广告不受日频控限制 |
| `feed` | 扫首页信息流 | 广告受账号画像影响严重（实测某个号 58% 是留学教育），且约 80 条后枯竭 |

**每条广告都带可点击链接**：脚本会进详情页点「分享 → 复制链接」，拿到
`https://xhslink.cn/xxx` 短链（手机上点开直接进 App）。取不到短链的会退化为站内搜索链接。
每条多花约 7 秒。

**关键词覆盖 = 数据覆盖面**。`android.search_keywords` 里按 13 个领域铺了 43 个词，
可自行增删。行业覆盖和条数都由它决定 —— 想多看某个领域，加词即可，无需改代码。

**品牌名会自动归一**：`core/brand.py` 先把「迈可适（maxicosi）」「maxicosi」「迈可适」
这类写法清洗合并，再交给模型判断中英文/简称是否同一品牌，结果缓存在
`data/brand_alias.json`（只在出现新名字时才调模型）。不做这步，「谁投得最多」这个指标会失真。

---

## 五、跑起来

**最短路径（只给行业名）**：

```
.\.venv\Scripts\python.exe run.py 跨境电商
```

`run.py` 会自动切到项目自带 `.venv`，并自动完成开机、采集、分析、传知识库、推群。
任何 AI 工具（Codex / Claude Code / WorkBuddy）读 `AGENTS.md` 后也只需要这一条命令。

> **先复制配置模板**：仓库里不含 `config.yaml`（里面有密钥，已被 `.gitignore` 排除），
> 第一次使用请复制模板再填自己的值：
>
> ```powershell
> Copy-Item config.example.yaml config.yaml
> ```

完整手工步骤：

```powershell
# 0. 自检（会告诉你还缺什么，务必先跑这个）
.\.venv\Scripts\python.exe doctor.py
.\.venv\Scripts\python.exe doctor.py --test-llm      # 顺带验证大模型能不能调通

# 1. 依赖（已自动装好，重装可用）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 先看效果（示例数据，不需要任何 key）
.\.venv\Scripts\python.exe run_daily.py --demo

# 3. 首次准备模拟器 + 登录小红书（只需一次，见下方说明）
.\.venv\Scripts\python.exe -m core.emulator

# 4. 小样验证能不能真抓到（不发群）
.\.venv\Scripts\python.exe run_daily.py --limit 5 --no-push

# 5. 正式跑（自动开机 → 自动开 App → 自动采集 → 传 IMA 知识库）
.\.venv\Scripts\python.exe run_daily.py --industry 跨境电商
```

### 全自动：不用先开模拟器

`emulator.auto_start: true`（默认）时，`run_daily.py` 自己完成整套准备：

1. 调 `MuMuManager.exe` 拉起模拟器 —— 安装路径从
   `%APPDATA%\Netease\MuMuPlayer\install_config.json` 自动读，换盘/升版本都不用改代码
2. 等 adb 端口从 `offline` 变成 `device`（只认 `device` 状态，避免抢跑）
3. 打开小红书
4. 检查登录态

**只有一种情况需要人工介入：账号被踢下线**（同一个号在手机上登录过）。
脚本会检测到「账号下线通知」，自动点「重新登录」把二维码调出来，
你用手机小红书扫一次就行 —— 之后只要不在手机上重新登录，就一直免扫码。

单独测这一步：

```powershell
.\.venv\Scripts\python.exe -m core.emulator         # 开机 + 开 App + 检测登录
.\.venv\Scripts\python.exe -m core.emulator --stop  # 关掉模拟器
```

返回码也有区分：`8` = 需要扫码登录，`9` = 模拟器没能启动。

### 需要你填的配置（`config.yaml`）

**大模型 API Key**

任何 OpenAI 兼容接口都行，改 `llm` 三行即可：

```yaml
llm:
  base_url: "https://api.deepseek.com/v1"   # 也可换成 dashscope / open.bigmodel / api.moonshot.cn
  api_key: "sk-xxxxxx"
  model: "deepseek-chat"
```

成本参考：100 条笔记 ≈ 100 次小请求，DeepSeek 下约 ¥0.2–0.5/天。

---

## 六、每天自动跑

Windows 计划任务（管理员 PowerShell 执行一次）：

```powershell
$proj = "C:\Users\wangdiao\Desktop\cc开发\搞薯条agent"
schtasks /Create /TN "XHS广告日报" /TR "\"$proj\run_daily.bat\"" /SC DAILY /ST 09:00 /F
```

- 日志：`data/logs/run.log`
- 电脑需处于开机/唤醒状态；笔记本建议在「电源选项」里允许唤醒执行
- 想换时间：任务计划程序里改，或重跑上面命令改 `/ST`

---

## 七、广告判定是怎么做的

小红书没有公开接口，所以走**多路信号融合**，判定依据会写进 CSV 那一列，方便你复核：

| 信号 | 来源 | 置信度 |
|---|---|---|
| 页面/接口上有「赞助 / 广告 / 品牌合作」标识 | DOM 标签 + 接口字段递归扫描 | 0.93 |
| 标题或正文出现强关键词（可配置） | 文案 | 0.78 |
| 命中 2 个以上弱关键词（优惠券/下单/直播间…） | 文案 | 0.62 |
| 只命中 1 个弱词 | 文案 | 0.45（记为疑似，不入库） |

**第一次跑完建议做这一步**：打开 `data/raw/` 里落下的搜索页 HTML，对照真实结构调
`config.yaml` 里的 `crawl.ad_rules`，命中率会明显提升。

调参方向：
- 抓不够 100 条 → 加 `crawl.keywords`、调大 `max_scroll_per_keyword`、把 `strong_keywords` 放宽
- 误判太多 → 提高 `require` 门槛（代码里 `_judge` 的两处阈值）
- 被风控/频繁要验证 → 调大 `min_delay`/`max_delay`，`headless` 保持 false

---

## 八、目录结构

```
搞薯条agent/
├── config.yaml                # 你的配置（密钥、行业名、知识库 ID）
├── run_daily.py               # 主流程入口
├── doctor.py                  # 自检：依赖 / 配置 / 登录态 / 连通性
├── ima_setup.py               # IMA 配置助手：列知识库、测上传
├── run_daily.bat              # 计划任务调用
├── core/
│   ├── config.py              # 配置与路径
│   ├── db.py                  # SQLite：笔记库 / 分析缓存 / 推送日志
│   ├── android_crawler.py     # ★ App 端采集（搜索、链路配额、三层去重、存活恢复）
│   ├── industry.py            # ★ 行业拆解 / 扩词 / 环节归属校验
│   ├── analyzer.py            # 大模型三轴抽取 + 标签归并 + 聚合
│   ├── brand.py               # 品牌名归一 + 按广告主/作者去重
│   ├── report.py              # HTML（折叠式）/ CSV / Markdown 三种产物
│   ├── ima.py                 # ★ 自动导入 IMA 知识库
│   ├── demo.py                # 示例数据
│   └── xhs_crawler.py         # 旧网页版抓取（已废弃，保留备查）
├── tools/
│   ├── ima/                   # IMA 官方上传脚本（ima_api / cos-upload / preflight）
│   └── android_probe.py       # 安卓端探测：能不能读到「广告」标签
├── legacy/                    # 已废弃的网页版脚本
│   ├── login_xhs.py           #   网页版扫码登录
│   └── probe_web.py           #   网页版诊断
└── data/
    ├── notes.db               # 历史笔记库（去重靠它）
    ├── brand_alias.json       # 品牌别名缓存
    ├── industry_plan_*.json   # 行业拆解计划（可手工编辑）
    ├── browser/profile/       # 安卓侧不需要；网页版登录态
    ├── raw/                   # 调试产物（_archive/ 是历史归档）
    ├── reports/               # 日报：{日期}.html / .csv / .md
    └── logs/run.log           # 计划任务运行日志
```

---

## 九、每天自动导入 IMA 知识库

每次跑完会自动把两份文本产物传进指定知识库：**Markdown 摘要**（含每条广告的三轴拆解，可被 IMA 检索问答）
和 **CSV 明细**。HTML 不支持——IMA 只允许在桌面端手动添加本地网页。

> 顺序是：生成报告（HTML/CSV/MD）→ 导入 IMA 知识库。IMA 失败不影响报告落盘。

**配置三步：**

1. 打开 https://ima.qq.com/agent-interface 生成 **Client ID** 和 **API Key**
2. 二选一存放：
   ```powershell
   # 方式 A：写进配置文件（简单）
   #   config.yaml → ima.client_id / ima.api_key
   # 方式 B：写进用户目录（不进 git，更安全）
   mkdir -Force "$env:USERPROFILE\.config\ima"
   "你的client_id" | Out-File "$env:USERPROFILE\.config\ima\client_id" -Encoding ascii
   "你的api_key"  | Out-File "$env:USERPROFILE\.config\ima\api_key"  -Encoding ascii
   ```
3. 列出知识库、拿 ID，然后测一次上传：

```powershell
.\.venv\Scripts\python.exe ima_setup.py          # 列出你的知识库
# 把 ID 填进 config.yaml 的 ima.knowledge_base_id，并把 ima.enabled 改成 true
.\.venv\Scripts\python.exe ima_setup.py --test    # 传一个测试文件验证
```

同名文件不会覆盖，会自动加时间戳后缀（`日报_20261022.md` → `日报_20261022173000.md`）。

**排查用：**

```powershell
.\.venv\Scripts\python.exe ima_setup.py --verify   # 看知识库里现在有什么
.\.venv\Scripts\python.exe doctor.py               # 看凭证/知识库 ID 是否就绪
```

常见错误 `{"code":200002,"msg":"skill auth failed"}` 基本都是 **Client ID 和 API Key 填反了**：
正确对应是 `client_id` = 32 位十六进制，`api_key` = 76 位 Base64（结尾带 `==`）。

---

## 十、遇到问题先看这里

### 1）报「IP存在风险，请切换可靠网络环境」（错误码 300012）

小红书把当前出口 IP 拉黑了。这是**网络问题，不是代码问题**。按顺序试：

1. 换回自己的家庭/办公宽带，**关掉 VPN、代理、加速器**（机房 IP 基本必封）
2. 确认 `crawl.headless` 是 `false`（无头模式更容易被判机器）
3. 在同一个网络下，先用普通 Chrome 打开小红书刷几分钟、确认能正常看笔记，再跑脚本
4. 仍然不行 → 走下面的方案 B

### 2）方案 B：连接你自己已登录的 Chrome（最稳）

```powershell
# 1. 双击运行，会开一个带调试端口的 Chrome
start_chrome_debug.bat

# 2. 在这个 Chrome 里正常登录小红书、随意浏览一下
# 3. 把 config.yaml 里 crawl.cdp_url 填上：
#    cdp_url: "http://127.0.0.1:9222"
# 4. 正常运行
.\.venv\Scripts\python.exe run_daily.py
```

这个模式复用了真实浏览器指纹和你手动登录的会话，风控通过率最高。注意脚本结束时**不会**关掉你的 Chrome。

### 3）抓到 0 条 / 数量偏少

**先跑诊断脚本**，它会告诉你广告标识到底出现在哪个页面、接口拦截有没有生效：

```powershell
.venv/Scripts/python.exe probe.py
```

输出写到 `data/raw/probe_report.txt`，重点看两行：

- `接口捕获: N 条` —— 为 0 说明响应拦截没生效，只有 DOM 数据
- `带广告标识: N 条` —— 为 0 说明这个页面当前不展示信息流广告

如果搜索页长期为 0，把 `crawl.sources` 改成 `["homefeed"]` 只用首页推荐流（广告密度更高），
或调大 `homefeed_scroll`。

### 4）IMA 里没看到文件

- 跑 `python ima_setup.py --verify` 看知识库里现在有什么
- 检查 `ima.enabled` 是否为 true、`ima.knowledge_base_id` 是否填对
- 报 `200002 skill auth failed`：Client ID 和 API Key 填反了（`client_id` 是 32 位十六进制，`api_key` 是 76 位 Base64）
- 同名文件会自动加时间戳，不会覆盖

### 5）分析报错 / 全是空标签

- `llm.api_key` 没填或填错；`base_url` 要与 key 的服务商匹配
- 模型不支持 `response_format: json_object`（部分小模型），把 `core/analyzer.py` 里那行删掉即可
- 报 429 说明限流，把 `llm.concurrency` 从 4 降到 1–2

---

## 十一、已知限制

1. **登录态会过期**：一般 7–30 天，或在手机上重新登录同一账号会把模拟器踢下线。
   程序会自动点「重新登录」把二维码调出来，用户扫一次即可（返回码 `8`）。
2. **风控**：单账号高频抓取可能触发验证码。建议日抓量控制在 100–200 条，间隔 ≥2 秒。
3. **IMA 不接收本地 HTML**（只支持在桌面端手动添加），所以每月自动上传的是 Markdown 摘要 + CSV；
   HTML 折叠式报告留在 `data/reports/` 本地查看。
4. 分析结果由大模型生成，存在误读，报告内已标注仅供投放参考。

---

## 十二、合规提醒

只抓取公开可见内容、仅做内部分析、控制频率、不二次分发原文与用户信息。
商业使用前请评估平台规则与《个人信息保护法》相关要求。

**强烈建议使用小号登录抓取。** 抓取请求带的是登录账号的身份，主账号一旦被限流，
影响的是你日常使用和账号权重；小号被限流的代价几乎为零。登录态仅存储在本地
`data/browser/profile/`，不会上传到任何地方。
