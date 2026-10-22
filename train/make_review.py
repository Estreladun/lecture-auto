"""第二步：生成人工复核页面。

只导出识别器自己没把握的样本（mid + low 两档），并把它给出的猜测作为提示显示。
提示对的直接回车，错的改一位，平均两三秒一张。

产物：
    train/out/review/index.html   用浏览器打开即可标注
    train/out/review/images/      待复核的图片
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>验证码标注</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 20px;
    background: #14161a; color: #e8eaed;
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  }
  .bar { width: min(560px, 92vw); }
  .meta { display: flex; justify-content: space-between; font-size: 13px; color: #9aa0a6; margin-bottom: 6px; }
  .track { height: 4px; background: #2a2e35; border-radius: 2px; overflow: hidden; }
  .fill { height: 100%; background: #4a9eff; transition: width .15s; }
  .card {
    background: #1c1f26; border: 1px solid #2a2e35; border-radius: 14px;
    padding: 28px 32px; display: flex; flex-direction: column; align-items: center; gap: 18px;
    box-shadow: 0 12px 40px rgba(0,0,0,.45);
  }
  img {
    width: 350px; height: 150px; image-rendering: pixelated; border-radius: 6px;
    background: #fff; object-fit: contain;
  }
  .answer {
    font-size: 46px; letter-spacing: 18px; font-variant-numeric: tabular-nums;
    font-family: ui-monospace, Consolas, monospace; min-height: 56px;
    color: #4a9eff; padding-left: 18px;
  }
  .answer.empty::after { content: "____"; color: #3a3f47; letter-spacing: 14px; }
  .hint { font-size: 13px; color: #6b7280; height: 18px; }
  .row { display: flex; gap: 10px; font-size: 13px; color: #9aa0a6; }
  kbd {
    background: #2a2e35; border: 1px solid #3a3f47; border-bottom-width: 2px;
    border-radius: 4px; padding: 1px 6px; font-family: inherit; font-size: 12px;
  }
  button {
    background: #2a2e35; color: #e8eaed; border: 1px solid #3a3f47;
    border-radius: 8px; padding: 8px 18px; cursor: pointer; font-size: 13px;
  }
  button:hover { background: #343a43; }
  button.primary { background: #2563eb; border-color: #2563eb; }
  button.primary:hover { background: #1d4ed8; }
  .bottom { display: flex; gap: 12px; align-items: center; }
  .done { font-size: 20px; line-height: 1.7; text-align: center; }
  .done b { color: #4a9eff; }
</style>
</head>
<body>
<div class="bar">
  <div class="meta"><span id="progress">0 / 0</span><span id="remain"></span></div>
  <div class="track"><div class="fill" id="fill" style="width:0%"></div></div>
</div>

<div class="card" id="card">
  <img id="pic" alt="验证码">
  <div class="answer empty" id="answer"></div>
  <div class="hint" id="hint"></div>
  <div class="bottom">
    <button id="skip">跳过</button>
    <button class="primary" id="ok">确认</button>
    <button id="undo">上一步</button>
  </div>
  <div class="row">
    <span><kbd>0-9</kbd> 输入</span>
    <span><kbd>Enter</kbd> 确认</span>
    <span><kbd>Backspace</kbd> 删除</span>
    <span><kbd>Tab</kbd> 采纳提示</span>
  </div>
</div>

<div class="card" id="finish" style="display:none">
  <div class="done">
    已标注 <b id="count">0</b> 张<br>
    导出后把文件放到 <code>train/out/reviewed.json</code>
  </div>
  <button class="primary" id="export">导出 JSON</button>
</div>

<script>
const ITEMS = __ITEMS__;
const KEY = "captcha-review-v1";
const state = JSON.parse(localStorage.getItem(KEY) || "{}");
let index = 0;
let typed = "";

const pic = document.getElementById("pic");
const answerEl = document.getElementById("answer");
const hintEl = document.getElementById("hint");
const progressEl = document.getElementById("progress");
const remainEl = document.getElementById("remain");
const fillEl = document.getElementById("fill");

function pending() {
  return ITEMS.filter(it => !(it.name in state));
}

function current() {
  const left = pending();
  return left.length ? left[0] : null;
}

function render() {
  const item = current();
  progressEl.textContent = `${Object.keys(state).length} / ${ITEMS.length}`;
  if (!item) {
    document.getElementById("card").style.display = "none";
    document.getElementById("finish").style.display = "flex";
    document.getElementById("count").textContent = Object.keys(state).length;
    fillEl.style.width = "100%";
    remainEl.textContent = "全部完成";
    return;
  }
  fillEl.style.width = (Object.keys(state).length / ITEMS.length * 100) + "%";
  remainEl.textContent = `剩余 ${pending().length}`;
  pic.src = "images/" + item.name;
  hintEl.textContent = item.hint ? `识别器猜测：${item.hint}` : "识别器没有给出结果";
  typed = "";
  paint();
}

function paint() {
  answerEl.textContent = typed;
  answerEl.classList.toggle("empty", typed.length === 0);
}

function commit(skip) {
  const item = current();
  if (!item) return;
  if (!skip) {
    if (typed.length !== 4) return;
    state[item.name] = typed;
  } else {
    state[item.name] = null;
  }
  localStorage.setItem(KEY, JSON.stringify(state));
  render();
}

document.addEventListener("keydown", event => {
  if (event.key >= "0" && event.key <= "9") {
    if (typed.length < 4) { typed += event.key; paint(); }
    event.preventDefault();
  } else if (event.key === "Backspace") {
    typed = typed.slice(0, -1); paint();
    event.preventDefault();
  } else if (event.key === "Enter") {
    if (typed.length === 4) commit(false);
    else if (typed.length === 0) {
      const item = current();
      if (item && item.hint) { typed = item.hint; paint(); }
    }
    event.preventDefault();
  } else if (event.key === "Tab") {
    const item = current();
    if (item && item.hint) { typed = item.hint; paint(); }
    event.preventDefault();
  }
});

document.getElementById("ok").onclick = () => commit(false);
document.getElementById("skip").onclick = () => commit(true);
document.getElementById("undo").onclick = () => {
  const done = Object.keys(state);
  if (!done.length) return;
  // 撤销最近标记的一张：把它从 state 里删掉即可重新出现
  const last = done[done.length - 1];
  delete state[last];
  localStorage.setItem(KEY, JSON.stringify(state));
  render();
};
document.getElementById("export").onclick = () => {
  const blob = new Blob([JSON.stringify(state, null, 1)], {type: "application/json"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "reviewed.json";
  link.click();
};

render();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成人工复核页面")
    parser.add_argument("--all", action="store_true",
                        help="导出全部样本（含高可信）用于整集校验")
    args = parser.parse_args()

    labels_path = HERE / "out" / "auto_labels.json"
    if not labels_path.exists():
        raise SystemExit("先运行 autolabel.py 生成 auto_labels.json")
    records = json.loads(labels_path.read_text(encoding="utf-8"))

    source = HERE / "data" / "images"
    target = HERE / "out" / "review" / "images"
    target.mkdir(parents=True, exist_ok=True)

    items = []
    for name, info in sorted(records.items()):
        if not args.all and info.get("tier") == "high":
            continue
        src = source / name
        if not src.exists():
            continue
        dst = target / name
        if not dst.exists():
            shutil.copy2(src, dst)
        items.append({"name": name, "hint": info.get("label") or ""})

    html = HTML.replace("__ITEMS__", json.dumps(items, ensure_ascii=False))
    page = HERE / "out" / "review" / "index.html"
    page.write_text(html, encoding="utf-8")

    print(f"待复核 {len(items)} 张")
    print(f"标注页面：{page}")
    print("用浏览器打开它，标注完点「导出 JSON」，把下载的文件放到 train/out/reviewed.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
