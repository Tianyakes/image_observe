"""设计系统审查 (基于 taste-skill / impeccable 的设计理念)。

analyze_page 深度审查的采集与判定: `_DESIGN_JS` 在浏览器内聚合指标,
`analyze_design` 在 Python 侧判定为 Finding (分区/名称/等级/依据/建议)。
与现有 `_LAYOUT_JS` 物理隔离 (现有 7 项检查逐字节不动), 零新依赖。

等级: info (参考) / warn (⚠ 建议修) / error (❌ 必须修)。
"""
_MAX_ELEMENTS = 800
_DESIGN_JS = r"""
(() => {
  const els = [...document.querySelectorAll('body *')].slice(0, __MAX__);
  const vw = innerWidth, vh = innerHeight;
  const out = { vw, vh };

  // ---- 颜色/对比度工具 (WCAG) ----
  function parseColor(c) {
    if (!c) return null;
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(',').map(s => parseFloat(s.trim()));
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    const h = c.match(/^#([0-9a-f]{6})$/i);
    if (h) { const n = parseInt(h[1], 16); return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 }; }
    return null;
  }
  function toHex(c) { return '#' + [c.r, c.g, c.b].map(v => Math.round(v).toString(16).padStart(2, '0')).join(''); }
  function lum(c) {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function contrast(a, b) {
    const la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  function visible(el, s) {
    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0.05;
  }
  const styles = new Map();
  function cs(el) { let s = styles.get(el); if (!s) { s = getComputedStyle(el); styles.set(el, s); } return s; }

  const typeScale = {}, lh = { defined: 0, total: 0 };
  const lineWidths = [], gaps = {}, headings = [], alignGroups = [], kickers = [], touchTargets = [];
  const colors = { text: {}, bg: {} }, btnContrast = [], radius = {}, shadows = {}, zs = [], anims = [];
  let cramped = 0, emdash = 0;

  for (const el of els) {
    const s = cs(el);
    if (!visible(el, s)) continue;
    const r = el.getBoundingClientRect();
    const fs = parseFloat(s.fontSize) || 0;
    const w = parseInt(s.fontWeight) || 400;
    const text = (el.textContent || '').trim();
    const isHeading = /^H[1-6]$/.test(el.tagName);

    // 1 排版阶梯 (heading/粗体字号直方图) + 行高定义率
    if (fs > 0 && (isHeading || w >= 600)) { const k = Math.round(fs); typeScale[k] = (typeScale[k] || 0) + 1; }
    if (el.tagName === 'P' || el.tagName === 'LI') { lh.total++; if (s.lineHeight !== 'normal') lh.defined++; }

    // 2 行宽估算 (仅收集可能超标的)
    if (r.width >= 200 && r.width <= 1400 && text.length >= 30 && fs >= 8 && fs <= 48
        && !el.closest('pre, code') && /^(P|LI|ARTICLE)$/.test(el.tagName)) {
      const cjk = (text.match(/[一-鿿]/g) || []).length;
      const factor = cjk > text.length - cjk ? 1.0 : 0.5;
      const chars = Math.round(r.width / (fs * factor));
      if (chars > 70) lineWidths.push({ chars, cjk: cjk > text.length - cjk, w: Math.round(r.width), tag: el.tagName, text: text.slice(0, 16) });
    }

    // 3 间距直方图 (面积达标元素) + cramped padding
    if (r.width * r.height >= 2000) {
      for (const side of ['Top', 'Bottom', 'Left', 'Right'])
        for (const prop of ['margin', 'padding']) {
          const v = parseFloat(s[prop + side]);
          if (v > 0) { const k = Math.round(v); (gaps[k] || (gaps[k] = { margin: 0, padding: 0 }))[prop]++; }
        }
      if (s.backgroundColor !== 'rgba(0, 0, 0, 0)' || s.borderTopWidth !== '0px') {
        const pv = (parseFloat(s.paddingTop) + parseFloat(s.paddingBottom)) / 2;
        const ph = (parseFloat(s.paddingLeft) + parseFloat(s.paddingRight)) / 2;
        if (pv < Math.max(4, 0.3 * fs) || ph < Math.max(8, 0.5 * fs)) cramped++;
      }
    }

    // 4 左缘对齐 (同父 ≥4 块级兄弟)
    if (el.children.length >= 4) {
      const lefts = [];
      for (const ch of el.children) {
        const ccs = cs(ch);
        if (ccs.display === 'inline' || ch.matches('img, svg, input, label, script, template')) continue;
        const cr = ch.getBoundingClientRect();
        if (cr.width > 120 && cr.top < vh + 200 && cr.top > -200) lefts.push(Math.round(cr.left));
      }
      if (lefts.length >= 4 && new Set(lefts).size > 3)
        alignGroups.push({ tag: el.tagName, cls: String(el.className || '').slice(0, 40), lefts: [...new Set(lefts)].sort((a, b) => a - b) });
    }

    // 5 heading 上方留白 > 下方
    if (isHeading && /^H[23]$/.test(el.tagName) && r.width > 0) {
      const isFirst = el.parentElement && el.parentElement.firstElementChild === el;
      headings.push({ tag: el.tagName, mt: Math.round(parseFloat(s.marginTop)), mb: Math.round(parseFloat(s.marginBottom)), first: isFirst, text: text.slice(0, 16) });
    }

    // 6 色彩直方图 + 按钮自身 vs 祖先背景对比度
    if (r.width * r.height >= 4) {
      if (text && s.color !== 'rgba(0, 0, 0, 0)') {
        const c = parseColor(s.color);
        if (c && c.a > 0.5) { const k = toHex(c); colors.text[k] = (colors.text[k] || 0) + 1; }
      }
      if (s.backgroundColor !== 'rgba(0, 0, 0, 0)' && !s.backgroundImage.includes('url') && !s.backgroundImage.includes('gradient')) {
        const c = parseColor(s.backgroundColor);
        if (c && c.a > 0.5) { const k = toHex(c); colors.bg[k] = (colors.bg[k] || 0) + 1; }
      }
      if (el.matches('button, [role="button"], input[type="submit"], a[class]') && r.width * r.height > 400) {
        const fg = parseColor(s.backgroundColor);
        if (fg && fg.a > 0.9) {
          let anc = el.parentElement, bg = null;
          while (anc) {
            const a = parseColor(cs(anc).backgroundColor);
            if (a && a.a > 0.9) { bg = a; break; }
            anc = anc.parentElement;
          }
          if (bg) { const ratio = contrast(fg, bg); if (ratio < 3.2) btnContrast.push({ ratio: +ratio.toFixed(2), text: text.slice(0, 16) }); }
        }
      }
    }

    // 7 圆角 / 阴影直方图
    const rad = parseFloat(s.borderTopLeftRadius);
    if (rad > 0) { const k = rad >= 1000 ? 'pill' : Math.round(rad); radius[k] = (radius[k] || 0) + 1; }
    if (s.boxShadow && s.boxShadow !== 'none' && r.width * r.height >= 400) {
      const m = s.boxShadow.match(/(\d+(?:\.\d+)?)px\s+(\d+(?:\.\d+)?)px\s+(\d+(?:\.\d+)?)px/);
      if (m) { const k = Math.round(parseFloat(m[3])); shadows[k] = (shadows[k] || 0) + 1; }
    }

    // 8 触摸目标 (排除正文内联文字链接)
    if (el.matches('a, button, input, select, [role="button"]') && s.pointerEvents !== 'none'
        && r.width > 8 && r.height > 8) {
      const inlineLink = el.tagName === 'A' && s.display === 'inline' && r.height < 28 && !el.querySelector('img, svg');
      if (!inlineLink) touchTargets.push({ tag: el.tagName, w: Math.round(r.width), h: Math.round(r.height), text: text.slice(0, 16) });
    }

    // 9 eyebrow 候选 (≤14px + 字重≥500 + uppercase 或宽 tracking)
    if (fs > 0 && fs <= 14 && w >= 500 && text) {
      const ls = parseFloat(s.letterSpacing) || 0;
      if (s.textTransform === 'uppercase' || (ls > 0.06 * fs && !/[a-z]/.test(text)))
        kickers.push({ y: Math.round(r.top), text: text.slice(0, 16) });
    }

    // 10 em-dash (p/li/heading 为叶子级文本容器)
    if (/^(P|LI|H[1-6])$/.test(el.tagName) && !el.closest('pre, code, blockquote'))
      emdash += (text.match(/—/g) || []).length;

    // 13 z-index
    if (s.zIndex && s.zIndex !== 'auto') zs.push(parseInt(s.zIndex, 10));
  }

  // 11 CTA 文案 (节区聚类在 Python 侧按 y 距离分组)
  const ctaTexts = [];
  for (const el of document.querySelectorAll('a, button')) {
    const s = cs(el);
    if (!visible(el, s)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (t.length < 2 || t.length > 30) continue;
    ctaTexts.push({ text: t, y: Math.round(r.top) });
  }

  // 12 动效 (getAnimations: 无限循环 / 布局属性 / 弹跳缓动)
  for (const a of document.getAnimations()) {
    const fx = a.effect;
    if (!fx || !fx.target || !fx.getKeyframes) continue;
    const kf = fx.getKeyframes();
    if (!kf.length) continue;
    const timing = fx.timing || {};
    const props = new Set();
    for (const f of kf) for (const k of Object.keys(f)) {
      if (k === 'offset' || k === 'easing' || k === 'composite') continue;
      props.add(k);
    }
    const tr = fx.target.getBoundingClientRect ? fx.target.getBoundingClientRect() : { width: 0, height: 0 };
    let bouncy = false;
    for (const f of kf) {
      const m = (f.easing || '').match(/cubic-bezier\(([^)]+)\)/);
      if (m) {
        const ys = m[1].split(',').map(s => parseFloat(s.trim()));
        if (ys.length === 4 && (ys[1] < 0 || ys[1] > 1 || ys[3] < 0 || ys[3] > 1)) bouncy = true;
      }
    }
    anims.push({
      infinite: timing.iterations === Infinity,
      size: Math.max(tr.width, tr.height),
      props: [...props],
      bouncy,
      duration: Math.round((timing.duration || 0) / 1000),
    });
  }

  // 15 hero 适配 (首屏内第一个高 ≥60vh 区块)
  const hero = { vh };
  let heroEl = null;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.height >= 0.6 * vh && r.top <= 40 && r.top >= -vh * 0.5) {
      if (!heroEl || r.top < heroEl.getBoundingClientRect().top) heroEl = el;
    }
  }
  if (heroEl) {
    const hr = heroEl.getBoundingClientRect();
    hero.top = Math.round(hr.top); hero.bottom = Math.round(hr.bottom);
    hero.tag = heroEl.tagName;
    hero.cls = String(heroEl.className || '').slice(0, 40);
    const h = heroEl.querySelector('h1, h2');
    if (h) { hero.titleLines = h.getClientRects().length || 1; hero.titleText = (h.textContent || '').trim().slice(0, 24); }
    const sub = heroEl.querySelector('p');
    if (sub) {
      const slh = parseFloat(getComputedStyle(sub).lineHeight) || 24;
      hero.subLines = Math.max(1, Math.round(sub.getBoundingClientRect().height / slh));
      hero.subWords = (sub.textContent || '').trim().split(/\s+/).length;
    }
  }
  const header = document.querySelector('header');
  if (header) hero.headerH = Math.round(header.getBoundingClientRect().height);

  out.typeScale = typeScale;
  out.lh = lh;
  out.lineWidths = lineWidths.slice(0, 8);
  out.gaps = gaps;
  out.cramped = cramped;
  out.alignGroups = alignGroups.slice(0, 5);
  out.headings = headings.slice(0, 10);
  out.colors = colors;
  out.btnContrast = btnContrast.slice(0, 8);
  out.radius = radius;
  out.shadows = shadows;
  out.touchTargets = touchTargets.slice(0, 12);
  out.kickers = kickers.slice(0, 12);
  out.emdash = emdash;
  out.ctaTexts = ctaTexts.slice(0, 25);
  out.anims = anims.slice(0, 10);
  out.zs = zs.slice(0, 20);
  out.hero = hero;
  return out;
})()
""".replace("__MAX__", str(_MAX_ELEMENTS))

_LAYOUT_ANIM_PROPS = {
    "width", "height", "top", "left", "right", "bottom", "inset",
    "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
    "padding", "padding-top", "padding-bottom", "padding-left", "padding-right",
    "font-size", "line-height", "border", "border-width", "gap",
}


def analyze_design(design: dict, depth: str = "standard", viewport_width: int = 1440) -> list[dict]:
    """把 `_DESIGN_JS` 采集的 design dict 判定为 Finding 列表。

    depth: "standard" (静态项全上) / "deep" (静态项 + hover, hover 需单独采集) /
           "light" (仅移动端敏感项, 供 analyze_responsive 用)。
    """
    findings: list[dict] = []
    vw = design.get("vw") or viewport_width
    desktop = vw > 768
    light = depth == "light"
    warn = "warn"
    add = lambda f: findings.append(f)  # noqa: E731

    # 1 排版阶梯 + 行高
    ts = design.get("typeScale") or {}
    sizes = [int(k) for k, n in ts.items() if n >= 2]
    if sizes and not light:
        mx, mn = max(sizes), min(sizes)
        if mx >= 20 and mx / mn < 2.0:
            add({"分区": "排版", "名称": "扁平层级", "等级": warn,
                 "依据": f"heading/粗体字号范围 {mn}~{mx}px, 比值 {mx / mn:.1f} < 2.0",
                 "建议": "拉大标题与正文的字号/字重差距, 建立清晰层级阶梯"})
    lh = design.get("lh") or {}
    if lh.get("total", 0) >= 4 and not light:
        defined = lh["defined"] / lh["total"]
        if defined < 0.6:
            add({"分区": "排版", "名称": "行高缺省", "等级": "info",
                 "依据": f"正文元素 {lh['defined']}/{lh['total']} 未显式设置 line-height",
                 "建议": "为正文统一设置行高 (1.5~1.7), 避免默认值节奏不一"})

    # 2 行宽
    for i, lw in enumerate(design.get("lineWidths", [])):
        if i >= 3:
            break
        limit = 50 if lw["cjk"] else 90
        if lw["chars"] > limit:
            add({"分区": "排版", "名称": "行宽超标", "等级": warn,
                 "依据": f"<{lw['tag']}> 每行约 {lw['chars']} 字符 (建议 {'≤50 (CJK)' if lw['cjk'] else '45-75'})",
                 "建议": "限制内容容器宽度, 提升可读性"})

    # 3 间距单调 + cramped padding
    gaps = design.get("gaps") or {}
    total = sum(v["margin"] + v["padding"] for v in gaps.values())
    if total >= 20:
        dom_key = max(gaps, key=lambda k: gaps[k]["margin"] + gaps[k]["padding"])
        dom = gaps[dom_key]["margin"] + gaps[dom_key]["padding"]
        if dom / total > 0.6 and len(gaps) <= 3:
            add({"分区": "布局", "名称": "间距单调", "等级": warn,
                 "依据": f"主导间距 {dom_key}px 占 {dom / total:.0%}, 去重仅 {len(gaps)} 个值",
                 "建议": "引入第二个间距尺规 (如 8/16/32 或 4 单位体系), 形成节奏"})
    if design.get("cramped", 0) > 0:
        add({"分区": "布局", "名称": "内边距局促", "等级": warn,
             "依据": f"{design['cramped']} 个有背景/边框的容器 padding 低于字号比例下限",
             "建议": "容器 padding 至少水平 max(8px, 0.5×字号)、垂直 max(4px, 0.3×字号)"})

    # 4 左缘对齐
    if not light:
        for g in design.get("alignGroups", [])[:3]:
            add({"分区": "布局", "名称": "左缘不对齐", "等级": warn,
                 "依据": f"<{g['tag']}> 子元素左缘 {len(g['lefts'])} 个不同值: {g['lefts'][:6]}",
                 "建议": "统一列/卡片左缘, 对齐到同一网格"})

    # 5 heading 留白
    for i, h in enumerate(design.get("headings", [])):
        if i >= 3:
            break
        if not h["first"] and h["mt"] < h["mb"]:
            add({"分区": "布局", "名称": "标题留白倒置", "等级": warn,
                 "依据": f"{h['tag']}「{h['text']}」上方 {h['mt']}px < 下方 {h['mb']}px",
                 "建议": "标题上方留白应大于下方, 让标题与所属内容成组"})

    # 6 色彩系统
    text_colors = (design.get("colors") or {}).get("text", {})
    if len(text_colors) > 6:
        top = sorted(text_colors, key=text_colors.get, reverse=True)[:6]
        add({"分区": "色彩系统", "名称": "色板膨胀", "等级": warn,
             "依据": f"显著文本色 {len(text_colors)} 种: {top}",
             "建议": "收敛到 1-2 个文本色 + 语义强调色, 保持单一 palette"})
    for bc in design.get("btnContrast", [])[:3]:
        add({"分区": "色彩系统", "名称": "控件对比度不足", "等级": warn,
             "依据": f"「{bc['text']}」按钮底色 vs 页面背景 {bc['ratio']}:1 (<3:1)",
             "建议": "加深按钮底色或加描边, 非文本控件也需 ≥3:1"})

    # 7 圆角 / 阴影
    radius = design.get("radius") or {}
    if len(radius) >= 5 and all(v >= 2 for v in radius.values()):
        add({"分区": "圆角与阴影", "名称": "圆角体系松散", "等级": warn,
             "依据": f"{len(radius)} 种圆角值: {list(radius)[:8]}",
             "建议": "收敛到单一圆角体系 (如 4/8/12 或全 pill)"})
    shadows = design.get("shadows") or {}
    if len(shadows) > 4:
        add({"分区": "圆角与阴影", "名称": "阴影体系松散", "等级": "info",
             "依据": f"{len(shadows)} 种阴影模糊值",
             "建议": "统一阴影档位 (如 1-2 档)"})

    # 8 触摸目标
    for i, t in enumerate(design.get("touchTargets", [])):
        if i >= 4:
            break
        d = min(t["w"], t["h"])
        if d < 32:
            add({"分区": "组件与交互", "名称": "触摸目标过小", "等级": "error",
                 "依据": f"{t['tag']}「{t['text']}」{t['w']}x{t['h']}px (<32px)",
                 "建议": "交互元素至少 44x44px"})
        elif d < 44:
            add({"分区": "组件与交互", "名称": "触摸目标偏小", "等级": warn if not desktop else "info",
                 "依据": f"{t['tag']}「{t['text']}」{t['w']}x{t['h']}px (标准 44px)",
                 "建议": "移动端交互元素应 ≥44x44px"})

    # 9 eyebrow 竞争 (同一 200px 视口带 ≥2 个 kicker)
    kickers = design.get("kickers") or []
    kickers.sort(key=lambda k: k["y"])
    clusters: list[list] = []
    for k in kickers:
        if clusters and k["y"] - clusters[-1][-1]["y"] <= 200:
            clusters[-1].append(k)
        else:
            clusters.append([k])
    for c in clusters:
        if len(c) >= 2:
            add({"分区": "组件与交互", "名称": "多个 eyebrow 竞争", "等级": warn,
                 "依据": f"同一视口带 {len(c)} 个 kicker 式小标题: {[x['text'] for x in c[:3]]}",
                 "建议": "每节最多一个 eyebrow, 弱化其余"})

    # 10 em-dash
    if design.get("emdash", 0) >= 4:
        add({"分区": "排版", "名称": "破折号滥用", "等级": warn,
             "依据": f"全页可见 {design['emdash']} 处 —",
             "建议": "用冒号/句号替代破折号"})

    # 11 重复 CTA (按 y 300px 距离聚类为节区)
    ctas = sorted(design.get("ctaTexts") or [], key=lambda c: c["y"])
    groups: list[list] = []
    for c in ctas:
        if groups and c["y"] - groups[-1][-1]["y"] <= 300:
            groups[-1].append(c)
        else:
            groups.append([c])
    dup_found: list[tuple[str, int]] = []
    for g in groups:
        by_text: dict[str, int] = {}
        for c in g:
            by_text[c["text"].lower()] = by_text.get(c["text"].lower(), 0) + 1
        for t, n in by_text.items():
            if n >= 2 and t not in [d[0] for d in dup_found]:
                dup_found.append((t, n))
    if dup_found:
        desc = "; ".join(f"「{t}」x{n}" for t, n in dup_found[:3])
        add({"分区": "组件与交互", "名称": "重复 CTA", "等级": warn,
             "依据": f"同节区重复文案: {desc}",
             "建议": "同一意图保留一个 CTA, 弱化或删除重复入口"})

    # 12 动效
    for i, a in enumerate(design.get("anims", [])):
        if i >= 6:
            break
        if a["infinite"] and a["size"] >= 24:
            add({"分区": "动效", "名称": "无限循环动画", "等级": "info",
                 "依据": f"{a['size']}px 元素, {a['duration']}ms, 无限循环",
                 "建议": "确认是否为品牌动效; 装饰性无限循环建议移除或尊重 prefers-reduced-motion"})
        layout_anim = [p for p in a["props"] if p in _LAYOUT_ANIM_PROPS]
        if layout_anim:
            add({"分区": "动效", "名称": "布局属性动画", "等级": warn,
                 "依据": f"动画属性含 {layout_anim[:4]}",
                 "建议": "只用 transform/opacity, 避免每帧重排掉帧"})
        if a["bouncy"]:
            add({"分区": "动效", "名称": "弹跳缓动", "等级": "info",
                 "依据": "cubic-bezier 越界 (y 超出 [0,1])",
                 "建议": "使用标准缓出曲线"})

    # 13 z-index
    zs = design.get("zs") or []
    if zs:
        uniq, mx = len(set(zs)), max(zs)
        if uniq > 5 or mx > 1000:
            add({"分区": "布局", "名称": "z-index 堆叠混乱", "等级": "info",
                 "依据": f"{uniq} 种 z-index, 最大值 {mx}",
                 "建议": "收敛 z-index 到少量层级 (如 0/10/100/1000)"})

    # 15 hero 适配
    hero = design.get("hero") or {}
    if hero.get("titleLines", 0) > 2:
        add({"分区": "组件与交互", "名称": "Hero 标题过长", "等级": warn,
             "依据": f"标题「{hero.get('titleText', '')}」{hero['titleLines']} 行",
             "建议": "hero 标题 ≤2 行, 超出截断或缩短文案"})
    if hero.get("subLines", 0) > 4 or hero.get("subWords", 0) > 20:
        add({"分区": "组件与交互", "名称": "Hero 副文过长", "等级": warn,
             "依据": f"副文 {hero.get('subLines', 0)} 行 / {hero.get('subWords', 0)} 词",
             "建议": "hero 副文 ≤4 行 ≤20 词"})
    if hero.get("bottom") and hero["bottom"] < hero.get("vh", 900) * 0.7:
        add({"分区": "组件与交互", "名称": "首屏未满", "等级": "info",
             "依据": f"首屏内容底部 {hero['bottom']}px (视口 {hero.get('vh')}px)",
             "建议": "撑满一屏或确认折叠线设计意图"})
    if hero.get("headerH", 0) > 80:
        add({"分区": "组件与交互", "名称": "导航过高", "等级": "info",
             "依据": f"header {hero['headerH']}px",
             "建议": "桌面导航高度 ≤80px 单行"})

    return findings


_HOVER_JS = r"""
() => {
  const out = [];
  let idx = 0;
  for (const el of document.querySelectorAll('a, button, [role="button"]')) {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    if (r.top < 0 || r.bottom > innerHeight || r.width < 8 || r.height < 8) continue;
    out.push({ i: idx++, tag: el.tagName, color: s.color, bg: s.backgroundColor, border: s.borderColor, text: (el.textContent || '').trim().slice(0, 12) });
    if (idx >= 10) break;
  }
  return out;
}
"""


async def collect_hover_states(page) -> list[dict]:
    """deep 模式: 采样视口内 ≤10 个交互元素 hover 前后的样式变化, 返回 Finding 列表。

    需要已渲染并停在目标视口的 Playwright page; hover 后移开鼠标, 不破坏页面状态。
    """
    findings: list[dict] = []
    try:
        before = await page.evaluate(_HOVER_JS)
    except Exception as e:
        return [{"分区": "组件与交互", "名称": "hover 采样失败", "等级": "info",
                 "依据": f"{e}", "建议": "忽略 hover 检查"}]
    for b in before:
        loc = page.locator(b["tag"]).nth(b["i"])
        try:
            await loc.hover()
            await page.wait_for_timeout(80)
            after = await loc.evaluate(
                """el => { const s = getComputedStyle(el); return { color: s.color, bg: s.backgroundColor, border: s.borderColor }; }"""
            )
            await page.mouse.move(2, 2)  # 移开, 避免悬停残留
        except Exception:
            continue
        changed = (after["color"], after["bg"], after["border"]) != (b["color"], b["bg"], b["border"])
        if not changed:
            findings.append({"分区": "组件与交互", "名称": "hover 无反馈", "等级": "warn",
                             "依据": f"{b['tag']}「{b['text']}」hover 前后样式无变化",
                             "建议": "为交互元素提供 hover 视觉反馈 (颜色/背景/边框)"})
    return findings
