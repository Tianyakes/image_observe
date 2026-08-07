## image-observe MCP(视觉验证工具)

源码在 `image_observe-main/`,升级记录见该目录 `EXECUTION_PLAN.md`(任何模型接手先读)。

- **13 个工具**:页面分析 `analyze_page`(布局诊断 + **设计系统审查**,`depth` 参数 quick/standard/deep,默认 standard)、`analyze_responsive`(三视口 + light 设计检查)、`inspect_element`、`diff_pages`(像素 diff)、`audit_page`(纯 DOM 无障碍)、**`aesthetic_audit`(美术审核:像素统计配色/构图/留白 + 调色板 + 美术审核员 0-100 分,查配色与大小比例严重失衡)**、`describe_image`/`extract_text`(OCR)、`search_images`(必应 + 批量视觉验证)、生成类 4 个
- **模型配置**(`image_observe-main/.env`,gitignored):`VISION_MODEL=doubao-seed-2-0-pro-260215` 主视觉模型,失败**自动 fallback** `VISION_MODEL_FALLBACK=doubao-seed-evolving`;`EMBEDDING_MODEL` 是文档预留,代码未接入
- **Kimi 兼容计划(下一步,未开发)**:目标兼容 Kimi API —— OpenAI Chat Completions 兼容(`base_url=https://api.moonshot.cn/v1`,Bearer 认证,openai SDK 直接可用,文档:https://platform.kimi.com/docs/api/overview 、https://platform.kimi.com/docs/api/quickstart)。方式:**单模型 `kimi-k2.5` 覆盖全部 MCP 工具**(区别于 doubao 的多模型方式:视觉理解/OCR/搜索验证/美术评审/生成类均由 kimi-k2.5 承担)。待办:config 增加 Kimi 切换(KIMI_BASE_URL/KIMI_API_KEY/KIMI_MODEL)、vision/search/aesthetic 的模型层抽象。状态:计划阶段,2026-08-07 记录,未开发
- **硬约定**(改动 image-observe 时必须遵守,详见 EXECUTION_PLAN.md §7):浏览器内 JS 采集 → Python 判定 → 中文【】分区输出;截图命名用 `unique_filename`(防并发覆盖);**视觉调用前必须已关闭浏览器**;search 输出首行 "匹配: 是/否" 格式不可破坏;视觉输出必须标注实际模型;`_LAYOUT_JS` 与 `design.py` 的 `_DESIGN_JS` 物理隔离
- **验证**:设计检查项用 `tests/fixtures/violations.html`/`clean.html`(`uv run python -m http.server 8000 --directory tests/fixtures` 服务后调 `analyze_page`);fallback 逻辑用 mock 脚本(不入库);编译检查 `uv run python -m py_compile src/image_observe/*.py`
