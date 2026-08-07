# image_observe

通用 **Agent 视觉接口** (MCP server): 任何 MCP 兼容的 Agent (Claude Code、Codex、Gemini CLI、Cursor 等) 都能连接使用, 提供媒体创作类视觉能力:

| 工具 | 能力 | 底层模型 |
|---|---|---|
| `generate_image` | 图像生成: 提示词生图, 返回 URL 并保存到本地 `output/` | Seedream 系列 |
| `edit_image` | 图像编辑: 自然语言指令修改图片 (消除/替换/风格/光影) | SeedEdit 3.0 |
| `generate_video` | 视频生成: 文生视频 / 首帧图生视频 (1~3 分钟), 保存到 `output/videos/` | Seedance 系列 / Wan2.1 |
| `generate_3d` | 3D 模型生成: 图生 3D (数分钟), 保存到 `output/3d/` | Seed3D 系列 |
| `describe_image` | 图片理解: 输入图片路径/URL, 返回文字描述 | 豆包视觉模型 (seed-2-0-pro) |
| `search_images` | 图片搜索: 网络搜图 + 视觉验证内容, 返回验证过的链接 | 必应国内版 + 豆包验证 |
| `analyze_page` | 网页分析: 渲染页面, 程序化布局诊断 (重叠/溢出/截断/字号/对比度) + 豆包视觉模型设计描述, 全部以文字返回, 截图存 `output/pages/` | Playwright + 豆包视觉 |
| `analyze_responsive` | 响应式分析: 375/768/1440 多视口逐档布局诊断 + 跨视口对比 (仅小屏出现的问题), 仅最宽视口调一次视觉模型 | Playwright + 豆包视觉 |
| `inspect_element` | 元素聚焦: 指定 CSS 选择器, 元素特写截图 + 几何/定位上下文 + 组件级视觉评审 | Playwright + 豆包视觉 |
| `diff_pages` | 视觉回归: 两个页面像素级 diff (占比/区域/包围盒) + 红标合成图 + 豆包描述新版变化 | Playwright + 豆包视觉 (浏览器内 canvas 计算) |
| `audit_page` | 无障碍审计: 纯程序化检查 alt/标题跳级/空文本链接按钮/表单标签/重复 id/歧义链接, 不调用视觉模型 | Playwright |
| `extract_text` | OCR: 从图片/截图逐行提取文字, 可选翻译 | 豆包视觉模型 |

## 前置条件

1. 火山方舟 API Key: [console.volcengine.com/ark](https://console.volcengine.com/ark) → API Key 管理
2. 在控制台 **开通管理** 中开通所用模型 —— 每个能力对应独立模型服务, **需逐个开通**:
   - 生图: `doubao-seedream-4-5-251128` (4-0/5-0-lite/5-0-pro 可选)
   - 编辑: `doubao-seededit-3-0-i2i`
   - 视频: `doubao-seedance-2-0-260128` (或 1.0/1.5/2.5 系列, i2v 模型见下)
   - 3D: `doubao-seed3d-2-0-260328` (或 1-0-250928)
   - 理解: 视觉模型 (如 `doubao-seed-2-0-pro-260215`), 主模型失败/未开通时自动切换备用 `doubao-seed-evolving` (均需开通)
   - 检索: `doubao-embedding-vision-251215` (图文多模态向量化, 供语义检索/以图搜图)
3. Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)
4. Edge 或 Chrome (二选一, `analyze_page` 渲染用, 二选一即可; 都没有则运行 `uv run playwright install chromium` 下载内置浏览器)

## 配置

```bash
cp .env.example .env   # 填入 ARK_API_KEY, 可按需改 VISION_MODEL / VISION_MODEL_FALLBACK / IMAGE_MODEL / EMBEDDING_MODEL
```

`.env` 已被 gitignore, 不会提交。被其他项目连接时, 也可以用 `--env` 直接传 key (见下)。

## 本地开发

```bash
uv sync                                  # 安装依赖
uv run python tests/smoke_client.py     # 冒烟测试: 生图 -> 视觉理解闭环
```

## 从 Claude Code 连接

```bash
claude mcp add image-observe \
  --env ARK_API_KEY=your_key \
  -- uv run --project "D:/AI Project/image_observe" python -m image_observe.server
```

> 提示: 若已在本项目 `.env` 配置好 key, 可省略 `--env`。`--project` 指向本项目路径, 因此在任何项目的 Claude Code 会话里都能连上。

其他 MCP 客户端 (Codex、Gemini CLI、Cursor、Claude Desktop) 同理, 在各自 MCP 配置中指向:

```
uv run --project <本项目路径> python -m image_observe.server
```

## 工具参数

- `generate_image(prompt, model=None, size="2K", watermark=True)` — model 可选 3-0/4-0/4-5/5-0-lite/5-0-pro; 3-0 需传像素值 (如 `2048x2048`)
- `edit_image(image, prompt, model=None, size="adaptive", watermark=True, scale=None)` — size 默认保持源图比例; scale 为指令强度 1~10
- `generate_video(prompt, model=None, image=None, ratio="16:9", resolution="720p", duration=None, generate_audio=True, watermark=False)` — 传 image 为首帧图生视频 (i2v 模型: `doubao-seedance-1-0-lite-i2v-250428`、`wan2-1-14b-i2v-250225`)
- `generate_3d(image, prompt=None, model=None, file_format="glb")` — file_format 支持 glb/obj/usd/usdz
- `describe_image(image, prompt=None)` — image 为本地绝对路径或 http(s) URL; prompt 为可选提问
- `search_images(query, count=5, verify=True)` — 自动翻译查询词, 多配方搜索并合并, 视觉模型逐张验证内容后返回链接; 适合无视觉能力的 agent 找图
- `analyze_page(url, viewport_width=1440, viewport_height=900, timeout=30)` — 渲染并分析网页: 程序化布局诊断 (元素重叠/横向溢出/内容截断/字号分布/WCAG 对比度, 不依赖视觉模型) + 豆包视觉模型设计描述; url 支持 http(s) 网址或本地 HTML 路径 / file://; 约需 10~60 秒, 截图保存到 `output/pages/`。适合无视觉能力的 agent 设计网页后自查渲染效果
- `analyze_responsive(url, viewports=None, timeout=30)` — 多视口响应式诊断 (默认 375x812/768x1024/1440x900): 逐档布局诊断 + 跨视口对比 + 仅小屏出现的问题标注; 视觉模型只调一次 (最宽视口)
- `inspect_element(url, selector, viewport_width=1440, viewport_height=900, prompt=None, timeout=30)` — 元素级聚焦分析: 特写截图 + 几何信息 + 最近定位祖先 + 组件级视觉评审; 选择器无效/未命中会给出候选提示
- `diff_pages(page_a, page_b, viewport_width=1440, viewport_height=900, tolerance=16, timeout=30)` — 像素级新旧对比 (改代码前后自查用): 差异占比/变更区域/包围盒 + 左旧右新红标合成图 + 豆包描述变化; **适合静态/本地页面, 含广告/时间戳/动画的动态页面会误报差异**
- `audit_page(url, timeout=30)` — 纯程序化无障碍审计, 不调用视觉模型: 图片 alt / 标题层级跳级 / 链接按钮空文本 / 表单控件标签 / 重复 id / 歧义链接
- `extract_text(image, language=None)` — 从图片逐行提取文字; language 提供时先提取原文再翻译为该语言

## 常见问题

- **报「模型未开通」** — 该模型未在火山方舟控制台「开通管理」中开通, 开通后立即可用 (每个能力独立开通)
- **视频/3D 生成很慢** — 异步任务制, 视频 1~3 分钟、3D 数分钟, 属正常
- **生成的媒体 URL 24 小时过期** — 本服务已自动下载到 `output/` 目录留存
- **analyze_page 报「无法启动浏览器」** — 本机未装 Edge/Chrome, 运行 `uv run playwright install chromium` 下载内置浏览器即可
- **diff_pages 差异过大** — 页面含广告/时间戳/动画等动态内容时会有差异误报, 该工具针对静态/本地页面; 若两次渲染本就不同 (如随机内容), 对比无意义
