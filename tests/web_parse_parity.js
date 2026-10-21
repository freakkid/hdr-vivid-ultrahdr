/**
 * 校验 web/index.html 里的解析逻辑与 Python 实现是否一致。
 *
 * 只测「解析」这一半（容器判定 / 增益图切片 / 元数据数值）——
 * 这部分是纯字节运算，与平台无关，能用 Node 严格对齐。
 * 像素烘焙那半依赖浏览器 canvas 解码，另见 tests/web_bake_parity.md。
 *
 * 用法: node tests/web_parse_parity.js <样张路径>
 */
"use strict";

const fs = require("fs");
const path = require("path");

const sample = process.argv[2];
if (!sample) {
  console.error("用法: node tests/web_parse_parity.js <样张路径>");
  process.exit(2);
}

const htmlPath = path.join(__dirname, "..", "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const scriptBody = html.split("<script>")[1].split("</script>")[0];
const CORE_MARK = "   5. UI 流程";
const cut = scriptBody.indexOf(CORE_MARK);
if (cut < 0) {
  console.error("未找到核心段落截止标记，index.html 结构可能已变");
  process.exit(2);
}
// 回退到该段注释的开头
const core = scriptBody.slice(0, scriptBody.lastIndexOf("/* ====", cut));

const stubEl = new Proxy(
  {},
  {
    get: (t, k) => {
      if (k === "classList") return { add() {}, remove() {}, toggle() {} };
      if (k === "querySelectorAll") return () => [];
      if (k === "appendChild") return () => {};
      if (k === "dataset") return {};
      return () => stubEl;
    },
    set: () => true,
  }
);
const documentStub = { querySelector: () => stubEl, createElement: () => stubEl };

const factory = new Function("document", core + "\nreturn { parseSource, findImages };");
const { parseSource, findImages } = factory(documentStub);

const buf = fs.readFileSync(sample);
const u8 = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);

const images = findImages(u8);
console.log(`文件: ${path.basename(sample)}  ${buf.length.toLocaleString()} B`);
console.log(`识别图像数: ${images.length}  ${JSON.stringify(images)}`);

const src = parseSource(u8);
console.log(`容器: ${src.kind} / ${src.container || "-"}`);
if (src.gainRaw) console.log(`增益图: ${src.gainRaw.length.toLocaleString()} B`);
if (src.meta) {
  console.log(`GainMapMin(log2)=${src.meta.min.toFixed(4)}  GainMapMax(log2)=${src.meta.max.toFixed(4)}`);
  console.log(`上限=${Math.pow(2, src.meta.max).toFixed(3)}x   载体=${src.meta.carrier}`);
  if (src.meta.note) console.log(`说明: ${src.meta.note}`);
}
if (src.warn) console.log(`警告: ${src.warn}`);
if (src.reason) console.log(`原因: ${src.reason}`);
