# image_observe 升级执行记录(供后续模型接力)

> 本文件记录 2026-08-07 一轮升级的**背景、改动清单、验证结果与遗留项**。
> 任何模型接手本项目时先读本文件 + `README.md`,再动手。

## 1. 背景与目标

用户基于 taste-skill / impeccable 两个设计 skill 的理念,要求 image-observe 更好地辅助主模型做前端设计审查,并修复代码审查发现的问题。三件事:

1. 记录执行计划(本文件)+ 更新 `../CLAUDE.md`,供别的模型接力
2. 补功能缺口:设计系统审查(排版阶梯/间距/色彩/圆角/触摸目标/动效/eyebrow/CTA/hero/hover)
3. 审查修复(H/M/L 项)+ 视觉模型自动 fallback

模型配置(火山方舟 Ark):
- `VISION_MODEL=doubao-seed-2-0-pro-260215`(主视觉模型)
- `VISION_MODEL_FALLBACK=doubao-seed-evolving`(备用,主模型失败自动切换)
- `EMBEDDING_MODEL=doubao-embedding-vision-251215`(**文档预留,代码未接入** —— 计划中的语义检索/以图搜图,勿误以为已实现)

## 2. 现状基线(升级后)

12 个 MCP 工具(server.py),全部注册于 MCPServer 0.4.0:

| 工具 | 能力 | 依赖 |
|---|---|---|
| describe_image / extract_text | 图片理解/OCR | 豆包视觉(自动 fallback) |
| analyze_page | 布局诊断 + **设计系统审查(depth)** + 加载监控 + 视觉描述 | Playwright + 豆包 |
| analyze_responsive | 多视口 + 跨视口对比 + **每视口 light 设计检查** | Playwright + 豆包 |
| inspect_element | 元素几何 + 特写评审 | Playwright + 豆包 |
| diff_pages | 像素 diff(降采样 1280)+ 合成图 | Playwright(无豆包依赖时降级) |
| audit_page | 6 项 DOM 无障碍,纯本地 | Playwright |
| search_images | 必应抓取 + **批量视觉验证**(多图合并) | 必应 + 豆包 |
| generate_image / edit_image / generate_video / generate_3d | 媒体生成 | Ark API |

模块职责(`src/image_observe/`):
- `server.py` MCP 注册层(参数校验在此 + page 入口)
- `page.py` 页面渲染/布局诊断/加载监控/设计审查接线
- `design.py` **设计系统审查**(采集 JS + Python 判定 + hover 采样)——本轮新增
- `vision.py` 视觉调用(异常分类/重试/fallback/模型标注)
- `search.py` 必应抓取 + 批量验证
- `ui_diff.py` 像素 diff
- `utils.py` 下载/轮询/文件名/图片编码
- `config.py` 模型与密钥配置

## 3. 本轮改动清单

### 3.1 修复(代码审查 H/M/L)
| 编号 | 改动 | 文件 |
|---|---|---|
| H1 | edit_image size 默认 None → edit.py 落 "adaptive" | server.py / edit.py |
| H2 | download() 改 httpx 流式 + 60s 超时 + 100MB 上限 + 失败清理 | utils.py |
| H3 | search 错误区分(API 错误 vs 内容不符)+ 批量验证 | search.py |
| H4 | 全部截图/下载文件名改 `unique_filename`(uuid+毫秒,防并发覆盖) | utils.py / page.py / ui_diff.py |
| H5 | wait_task 瞬时错误退避重试(仅失败终态终止) | utils.py |
| M1 | 工具入口参数校验(viewport/timeout/tolerance/count/selector/depth),中文错误 | server.py / page.py / ui_diff.py |
| M2 | 渲染稳定:fonts.ready 等待(3s 尽力)+ 固定等待降至 500ms | page.py |
| M3 | page.evaluate 异常包装中文 | page.py |
| M4 | responsive 每视口 `_layout_signals` 缓存 | page.py |
| M6 | diff 像素降采样(最长边 1280) | ui_diff.py |
| M7 | 视觉调用前关闭浏览器(截图落盘后) | page.py / ui_diff.py |
| M8 | 模型默认值统一(pro),README/.env.example 同步 | config.py / README / .env.example |
| M9 | 必应抓取 follow_redirects + 无扩展名 URL 放宽 | search.py |
| M10 | 报告披露元素采样上限 400 | page.py |
| M11 | video/three_d 的 print → logging | video.py / three_d.py |
| L1/L2/L8 | vision 输入校验、空内容守卫、format 注入修复 | vision.py |
| L4 | 魔法数字提取模块级常量 | page.py / ui_diff.py |
| L5 | 版本统一 0.4.0 | __init__.py / pyproject.toml |
| L9 | alpha=0 文本跳过对比度判定 | page.py |

### 3.2 vision fallback(核心)
`vision.py`:
- `_classify_error`: **fallback**(ModelNotOpen/InvalidEndpointOrModel.NotFound/404/空内容)→ **retry**(429/5xx/网络/超时)→ **stop**(401/403/图片参数 400/输入错误)
- `_chat_with_policy(messages)`:主模型 1+3 退避重试(1/2/4s)→ 切 `VISION_MODEL_FALLBACK`(2 次尝试);每调用 90s 超时,总预算 240s
- 错误卫生:不回显 key;双失败列出两模型各自原因
- 成功输出末尾标注 `(视觉模型: X)`,暴露 fallback 或配置拼写错误
- **约定:search 的批量验证不走 describe_image**(用 `vision._chat_with_policy`),避免标注破坏"匹配: 是/否"解析

`search.py`:
- `_verify_batch`:一条消息多图(10 张起步,400 自动拆半,最小 4),prompt "第N张: 匹配: 是/否"
- 整批 API 错误二分拆批,单张仍失败冒泡报错(不再静默当"内容不符")
- 并发 2 workers;调用量从最多 45 次降到 ~5-12 次

### 3.3 设计系统审查(design.py,本轮新增)
`analyze_page` 新增 `depth` 参数(quick/standard/deep,默认 standard):

| # | 检查项 | 等级 | 判定规则 |
|---|---|---|---|
| 1 | 扁平层级 | ⚠ | heading/粗体字号 max/min < 2.0(且 max≥20px) |
| 1b | 行高缺省 | info | 正文 line-height 定义率 <60% |
| 2 | 行宽超标 | ⚠ | Latin >90 字符/行, CJK >50(CJK 因子 1.0) |
| 3 | 间距单调 | ⚠ | 主导间距 >60% 且去重 ≤3 个值 |
| 3b | 内边距局促 | ⚠ | padding < max(4px,0.3×字号) 或 max(8px,0.5×字号) |
| 4 | 左缘不对齐 | ⚠ | 同父 ≥4 兄弟左缘去重 >3 个值 |
| 5 | 标题留白倒置 | ⚠ | h2/h3 上方 margin < 下方 |
| 6 | 色板膨胀 | ⚠ | 显著文本色 >6 种 |
| 6b | 控件对比度不足 | ⚠ | 按钮底色 vs 祖先背景 <3.0:1 |
| 7 | 圆角/阴影体系松散 | ⚠/info | 圆角 ≥5 种且各 ≥2 元素用;阴影模糊 >4 种 |
| 8 | 触摸目标过小/偏小 | ❌/⚠ | <32px ❌, 32-44 ⚠(桌面 advisory) |
| 9 | eyebrow 竞争 | ⚠ | 同一 200px 窗口 ≥2 个 kicker(≤14px+uppercase+tracking) |
| 10 | 破折号滥用 | ⚠ | 全页 ≥4 处 — |
| 11 | 重复 CTA | ⚠ | 同节区(y≤300px)同文案 ≥2 |
| 12 | 布局属性动画/弹跳/无限 | ⚠/info | getAnimations 分析(width/height 等布局属性动画) |
| 13 | z-index 堆叠混乱 | info | 去重 >5 或 max>1000 |
| 14 | hover 无反馈(deep) | ⚠ | 采样 ≤10 交互元素 hover 前后样式无变化 |
| 15 | Hero 标题/副文过长 | ⚠ | 标题 >2 行、副文 >4 行或 >20 词;header>80px info |

- `analyze_responsive` 每视口以 light 模式复用(仅移动端敏感项:触摸/对比度/CTA/间距/色板/hero)
- 输出结构(只增不改):【布局诊断】→【排版】【布局】【色彩系统】【圆角与阴影】【组件与交互】【动效】【加载与渲染】→【视觉描述】→【截图已保存】
- **交叉验证**:视觉 prompt 附最严重 2-3 条程序化发现,要求模型确认/反驳/补充;输出标注 `(视觉模型: X)`
- **加载监控**:console error/pageerror/requestfailed/HTTP≥400/字体失败/图片失败(naturalWidth=0)

## 4. 验证方案与结果

### fixtures
- `tests/fixtures/violations.html`:故意违规(扁平层级/破折号/重复 CTA/28px 按钮/hover 无反馈/布局动画/弹跳/坏字体/坏图/console.error/未捕获 throw)
- `tests/fixtures/clean.html`:合规对照
- 服务方式:`uv run python -m http.server 8000 --directory tests/fixtures`

### 已执行验证(2026-08-07,全部通过)
| 项目 | 结果 |
|---|---|
| vision fallback mock 测试(主成功/切备用/429 退避/空内容/双失败/401 stop) | 6/6 通过 |
| violations.html depth=deep 375px | 布局 4 类 + 设计 11 类 + 加载 6 类 + 视觉确认,全命中 |
| clean.html standard 1440px | 仅扁平层级(19~32px)与内边距提示,无严重误报 |
| analyze_responsive 三视口 | light 检查正常 + 跨视口对比正常 |
| diff_pages | 降采样生效, diff/合成图/视觉正常 |
| 参数校验 | 中文错误正常 |
| search_images(必应+批量) | 修复 302 后待最终确认(见遗留项) |

### 验证命令
```bash
cd image_observe-main
uv run python -m py_compile src/image_observe/*.py
uv run python -m http.server 8000 --directory tests/fixtures   # 起 fixtures
# 直接调 page 函数(不走 MCP 层)或通过 MCP 客户端调用
```

## 5. 风险与边界(已知)

- **搜索验证**:`search_images` 依赖必应 HTML 结构(易碎),批量验证依赖豆包对"第N张"格式的遵循;解析失败时该张视为不通过(保守)
- **动效检查**:只报信息级,排除 <24px spinner;getAnimations 在 Chrome 行为稳定
- **hover(deep)**:只采样视口内元素,移动端无 hover 语义,desktop 才有价值
- **行宽估算**:字符数 = 宽度/(字号×因子) 是估算,CJK 因子 1.0、Latin 0.5;code/pre 跳过
- **色板膨胀**:alpha<1 跳过(混合未知);背景图/渐变跳过
- **文件下载**:httpx 流式,60s 超时,100MB 上限——超限/失败抛中文错误

## 6. 遗留项(未做,按优先级)

1. **search_images 全链路回归**:必应抓取已通(302 修复);批量验证请求已发出,但 Ark 端抓取部分外站图(wikimedia)超时,属外部 URL 可达性,错误已正确区分上报。候选 URL 过慢时可考虑 httpx 预检或转 data URL
2. **M5 浏览器进程级单例**:每次调用冷启动 ~1-3s;做时注意异常路径 try/finally 与 fallback 期间不持浏览器
3. **断点行为检测**(matchMedia 分析)——analyze_responsive 跨视口已覆盖主要价值,排下一轮
4. **重叠语义(z-index 参与判定)** 与 **focus 状态检查**(键盘遍历)——成本高、误报大,暂缓
5. **EMBEDDING_MODEL 接入**:search.py 语义检索/以图搜图(需 embedding 数据库),文档先行状态
6. 测试自动化:目前 fixtures + 临时脚本验证,可考虑 pytest 化(mock 浏览器/API)

## 7. 硬约定(维护时必须遵守)

- 浏览器内 JS 采集 → Python 判定 → 中文输出【】分区
- 产物命名用 `utils.unique_filename`(防并发覆盖)
- **视觉调用前必须已关闭浏览器**(M7)
- **search 输出首行 "匹配: 是/否" 格式不可破坏**(`_verify_batch` 解析依赖)
- 视觉成功输出必须标注实际模型 `(视觉模型: X)`
- `_LAYOUT_JS`(page.py)与 `_DESIGN_JS`(design.py)物理隔离,互不依赖;新增检查进 design.py
- 不加新依赖(httpx/openai/playwright 已有)、不改传输协议
