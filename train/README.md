# 验证码识别模型训练

给主项目训练一个专用的四位数字验证码识别模型，替代通用的 ddddocr。

## 为什么要训

ddddocr 是 8000 多类的通用 OCR，用它认四位数字等于拿大炮打蚊子：

- 会把 `8` 认成 `s`、`0` 认成 `O`（输出含字母）
- 8000 类 softmax 里只有 10 个是我们要的，其余全是干扰
- 模型 30MB，单张 20-30ms

专用模型只有 **242K 参数**、几百 KB，单张 **1-2ms**，而且天然只输出数字。

## 数据来源

两部分：

| 来源 | 数量 | 标签质量 |
|---|---|---|
| [GT-ZhangAcer/CaptchaDataset](https://github.com/GT-ZhangAcer/CaptchaDataset)（MIT） | 9453 | 人工审核 |
| 本项目采集 | 1023 | ddddocr 伪标签，取高可信部分 623 张 |

外部数据集采自河南部分高校登录页，**图片尺寸同为 70×30，风格与东大一致**——实测现有识别器在它上面准确率 97.5%，属于同一类验证码组件，因此这批干净标签可以直接用。

> 注意：换成风格完全不同的验证码（不同字体/配色/干扰线）时，一定要重新评估域差异，否则混训反而有害。

## 流程

### 1. 装依赖

训练需要 PyTorch，用你已有的环境跑即可（不必装进项目 venv）：

```bash
E:\Miniforge3\envs\ai_base\python.exe -c "import torch; print(torch.__version__)"
```

### 2. 下载并导入外部数据集

```bash
git clone --depth 1 https://github.com/GT-ZhangAcer/CaptchaDataset.git train/data/external_dataset
uv run python train/import_external.py
```

### 3. 给本地样本打伪标签

```bash
uv run python train/autolabel.py
```

会输出三档：高可信 60.9%、待复核 25.1%、必标注 14.0%。

### 4.（可选）人工复核

```bash
uv run python train/make_review.py
```

用浏览器打开 `train/out/review/index.html`，按提示标注，导出 JSON 放到 `train/out/reviewed.json`。

这一步的收益：本地样本的标签从"机器猜的"升级为"人看过的"。**没做也能训练**，只是本地部分仍受伪标签质量限制。

### 5. 合并标签并训练

```bash
uv run python train/merge_labels.py
E:\Miniforge3\envs\ai_base\python.exe train/train.py --epochs 40 --patience 5
```

CPU 上约 8-12 分钟。`--patience` 是早停轮数：连续 N 轮验证集没有提升就停下，避免无效训练。

训练会先跑交叉校验（见下）发现的问题针对性调增强，当前 `augment()` 包含：

| 增强 | 作用 |
|---|---|
| 深色干扰线 1-2 条 | 模拟真实验证码的划线，修 `2` 被斜线穿过时认成 `7` 的问题 |
| 旋转 ±6° / 平移 ±2.5px / 缩放 0.9~1.1 | 字符位置与粗细的自然差异 |
| 亮度 / 对比度 / 高斯噪声 | 截图压缩与干扰强度变化 |

不做镜像翻转——数字翻转后不再是合法数字。

### 6. 交叉校验（可选但建议）

```bash
uv run python train/crosscheck.py
```

用训练好的模型反过来查训练标签，两边不一致的地方就是可疑项。也可以用来判断“伪标签到底干不干净”。

### 7. 评估与导出

```bash
E:\Miniforge3\envs\ai_base\python.exe train/evaluate.py
E:\Miniforge3\envs\ai_base\python.exe train/export_onnx.py
```

导出的 `seu_lecture/models/captcha.onnx` 会被主项目自动优先使用，推理走 onnxruntime——**不需要安装 torch**。

## 实测结果

| 指标 | ddddocr（原方案） | 本模型 |
| --- | --- | --- |
| 整串准确率 | 93%（14 张标注） | 见训练日志 |
| 参数量 | 30MB 模型 | 242K / 0.9MB |
| 单张推理 | 20-30ms | 1-2ms |
| 是否输出字母 | 会（`066s`、`967O`） | 不会 |

## 目录

```
train/
  model.py            网络定义（4 个十选一分类头，宽度方向压成 4 段）
  data.py             数据集与增强（仿射 + 光度，不做镜像）
  autolabel.py        伪标签 + 置信度分级
  make_review.py      生成人工复核页面
  merge_labels.py     合并标签
  train.py            训练
  evaluate.py         评估 + 测速
  export_onnx.py      导出 ONNX
  data/images/        训练图片（不入库）
  out/                中间产物（不入库）
  checkpoints/        权重
```

## 数据集未入库

`data/images/`、`data/external_dataset/`、`out/` 都在 `.gitignore` 里——数据和中间产物不进仓库，按上面步骤本地生成即可。

## 引用

```
GT-ZhangAcer/CaptchaDataset (MIT License)
https://github.com/GT-ZhangAcer/CaptchaDataset
采自河南部分高校网站登录页，四位数字图像 9453 张，标注经人工审核。
```
