/* AM —— 共享动画词汇表（seek 捕获安全版）
 * 铁律（实弹验证的可靠模式，违者必炸）：
 *  1. 一律 fromTo，目标传【字符串选择器】（GSAP seek 时惰性解析）。
 *     ✗ 禁止把 querySelectorAll 收集的元素数组直接喂给 GSAP
 *     （build 时刻收集的引用数组在 seek 捕获下 opacity 会静默失效）。
 *  2. 多目标错峰用 gsap stagger，不用手动 forEach。
 *  3. 循环用固定 repeat 数（确定性）；元素存在性用 q() 预判。
 *  4. draw() 是唯一例外：strokeDashoffset 需逐元素测长，用引用安全（属性 tween 不受此坑）。
 */
window.AM = (function () {
  function q(sel) { return document.querySelector(sel); }

  /* ── 进场 ─────────────────────────────────────────────── */

  // 弹性过冲（节点/徽章/图标）：scale 0.55→1 带轻微回弹
  function pop(tl, sel, at, dur) {
    tl.fromTo(sel, { opacity: 0, scale: 0.55 },
      { opacity: 1, scale: 1, duration: dur || 0.5, ease: "back.out(2.4)", stagger: 0.08 },
      at);
  }

  // 遮罩升起（标题/卡片：外层容器需 overflow:hidden）
  function maskRise(tl, sel, at, dur, stagger) {
    tl.fromTo(sel, { yPercent: 115 },
      { yPercent: 0, duration: dur || 0.75, ease: "power4.out", stagger: stagger || 0.1 },
      at);
  }

  // 边缘擦入（clip-path inset，杂志感强）：edge=top|bottom|left|right
  var _WIPE = {
    top:    ["inset(0 0 100% 0)", "inset(0 0 0% 0)"],
    bottom: ["inset(100% 0 0 0)", "inset(0% 0 0 0)"],
    left:   ["inset(0 100% 0 0)", "inset(0 0% 0 0)"],
    right:  ["inset(0 0 0 100%)", "inset(0 0 0 0%)"],
  };
  function wipe(tl, sel, at, dur, edge) {
    var pair = _WIPE[edge || "left"];
    tl.fromTo(sel, { clipPath: pair[0] },
      { clipPath: pair[1], duration: dur || 0.7, ease: "power3.inOut", stagger: 0.1 },
      at);
  }

  // 模糊聚焦（大数字/图片：先虚后实 + 微缩放归位）
  function focus(tl, sel, at, dur) {
    tl.fromTo(sel, { opacity: 0, filter: "blur(14px)", scale: 1.06 },
      { opacity: 1, filter: "blur(0px)", scale: 1, duration: dur || 0.85,
        ease: "power2.out", stagger: 0.08 },
      at);
  }

  // SVG 连线画出（stroke-dashoffset：flow/journey/fishbone 的路径生长）
  function draw(tl, sel, at, dur, stagger) {
    var els = Array.prototype.slice.call(document.querySelectorAll(sel));
    els.forEach(function (el, i) {
      if (!el || !el.getTotalLength) return;
      var L = el.getTotalLength();
      el.style.strokeDasharray = L;
      el.style.strokeDashoffset = L;
      tl.fromTo(el, { strokeDashoffset: L },
        { strokeDashoffset: 0, duration: dur || 0.8, ease: "power2.inOut" },
        at + i * (stagger || 0.14));
    });
  }

  // 数字滚动（大数字/统计：from→to 逐帧写入 textContent）
  function countUp(tl, sel, from, to, at, dur, suffix) {
    var el = q(sel); if (!el) return;
    var o = { v: from };
    tl.fromTo(o, { v: from },
      { v: to, duration: dur || 1.2, ease: "power3.out",
        onUpdate: function () {
          el.textContent = Math.round(o.v).toLocaleString("en-US") + (suffix || "");
        } }, at);
  }

  /* ── 氛围（进场后的持续生命感）──────────────────────────── */

  // Ken Burns（图片整镜慢推：scale 1.02→1.12 + 微位移）
  function kenburns(tl, sel, D) {
    tl.fromTo(sel, { scale: 1.02, xPercent: 0 },
      { scale: 1.12, xPercent: -1.8, duration: D, ease: "none", stagger: 0 }, 0);
  }

  // 呼吸强调（当前重点元素：轻微缩放起伏，固定轮次）
  function pulse(tl, sel, at, dur) {
    tl.fromTo(sel, { scale: 1 },
      { scale: 1.04, duration: dur || 0.7, ease: "sine.inOut", yoyo: true,
        repeat: 6, stagger: 0.1 },
      at);
  }

  // 扫光（高亮条/下划线：一条斜向高光从左掠过）
  function shimmer(tl, sel, at, dur) {
    tl.fromTo(sel, { xPercent: -130 },
      { xPercent: 240, duration: dur || 1.1, ease: "power2.inOut", stagger: 0 }, at);
  }

  // 漂浮（角标/装饰图标：缓慢上下起伏）
  function float(tl, sel, at, D) {
    tl.fromTo(sel, { y: 0 },
      { y: -7, duration: 2.4, ease: "sine.inOut", yoyo: true,
        repeat: Math.min(12, Math.ceil((D || 6) / 2.4)), stagger: 0.35 },
      at || 0.4);
  }

  /* ── 进阶元素（第二批，108 模板时代扩充）──────────────── */

  // 逐项淡入（列表/卡片：统一 y+opacity 错峰，最通用的入场）
  function stagger(tl, sel, at, dur, st) {
    tl.fromTo(sel, { opacity: 0, y: 22 },
      { opacity: 1, y: 0, duration: dur || 0.55, ease: "power3.out", stagger: st || 0.09 }, at);
  }

  // 横向滑入（dir=-1 左进 / 1 右进）
  function slideX(tl, sel, at, dur, dir, st) {
    var d = (dir || 1);
    tl.fromTo(sel, { opacity: 0, x: 56 * d },
      { opacity: 1, x: 0, duration: dur || 0.65, ease: "power3.out", stagger: st || 0.08 }, at);
  }

  // 生长线（进度条/分隔线：scaleX 0→1，需在 CSS 里给 transform-origin:left）
  function growX(tl, sel, at, dur, st) {
    tl.fromTo(sel, { scaleX: 0, opacity: 0.6 },
      { scaleX: 1, opacity: 1, duration: dur || 0.8, ease: "power3.inOut", stagger: st || 0.1 }, at);
  }

  // 盖章（印章/认证：放大下砸 + 落定微倾，冲击感）
  function stamp(tl, sel, at, dur) {
    tl.fromTo(sel, { opacity: 0, scale: 2.6, rotation: -16 },
      { opacity: 1, scale: 1, rotation: -6, duration: dur || 0.45, ease: "power4.in" }, at);
  }

  // 轨道环绕（卫星/装饰：绕自身中心匀速转固定角度；配合外层容器定位）
  function orbit(tl, sel, at, D, deg) {
    tl.fromTo(sel, { rotation: 0 },
      { rotation: (deg === undefined ? 360 : deg), duration: D || 6, ease: "none",
        transformOrigin: "50% 50%", stagger: 0 }, at || 0);
  }

  // 走马灯（滚动条/公告：匀速平推，距离按内容宽度给）
  function marquee(tl, sel, at, D, dist) {
    tl.fromTo(sel, { x: 0 },
      { x: -(dist || 900), duration: D || 8, ease: "none", stagger: 0 }, at || 0);
  }

  // 光标闪烁（终端/输入框：固定轮次，确定性）
  function blink(tl, sel, at) {
    tl.fromTo(sel, { opacity: 1 },
      { opacity: 0, duration: 0.46, ease: "steps(1)", yoyo: true, repeat: 11, stagger: 0 },
      at || 0);
  }

  // 涟漪（强调点：圆环外扩消散，多次错峰）
  function ripple(tl, sel, at, dur) {
    tl.fromTo(sel, { scale: 0.5, opacity: 0.7 },
      { scale: 2.2, opacity: 0, duration: dur || 1.0, ease: "power1.out", stagger: 0.35 }, at);
  }

  // 展开（票券/卡片：3D 翻起落定，需父级 perspective 或内置 transformPerspective）
  function unfold(tl, sel, at, dur) {
    tl.fromTo(sel,
      { opacity: 0, rotationX: -70, y: 26, transformPerspective: 900, transformOrigin: "50% 0%" },
      { opacity: 1, rotationX: 0, y: 0, duration: dur || 0.7, ease: "power3.out", stagger: 0.1 }, at);
  }

  /* ── 出场（为转场让路，非全隐）──────────────────────────── */

  function exitSoft(tl, sel, D) {
    tl.fromTo(sel, { opacity: 1, y: 0 },
      { opacity: 0.4, y: -10, duration: 0.45, ease: "power2.in", stagger: 0.04 },
      Math.max(0, D - 0.55));
  }

  return { q: q, pop: pop, maskRise: maskRise, wipe: wipe, focus: focus,
           draw: draw, countUp: countUp, kenburns: kenburns, pulse: pulse,
           shimmer: shimmer, float: float, exitSoft: exitSoft,
           stagger: stagger, slideX: slideX, growX: growX, stamp: stamp,
           orbit: orbit, marquee: marquee, blink: blink, ripple: ripple,
           unfold: unfold };
})();
