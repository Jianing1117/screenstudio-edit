---
name: screenstudio-edit
description: 剪辑可编辑的 Screen Studio 录屏工程，清理空白、失败重录和口误，并为鼠标指示的小字添加原生缩放，保留音画同步与已有人工修改。用于 .screenstudio 初剪、局部返修及续剪；普通 MP4 剪辑、字幕、抠像和 PPT 换屏不属于此流程。
---

# Screen Studio 初剪

统一入口 `scripts/run.sh`，输出可精修的副本。缺环境先运行 `scripts/setup.sh`（需要 Python 3.11+ 和 ffmpeg）。改进本 skill 时才读 [开发与测试](references/development.md)。

## 分工与标准

AI 做四件事：
1. 首次通读 `transcript.txt`。
2. 写语义计划：失败重录按组选择完整流畅的一遍，后文须覆盖被删信息。分别核对口播和演示覆盖，保护独有操作、数字、限定条件、提醒、有意强调及同一例子的不同解释作用。不能靠词表或相似度自动删有声内容。
3. 用 `pauses --status auto --activity-only` 复核可执行停顿中的操作和结果展示；需要读屏则在计划写 keep。分页查询只看相关项。
4. 执行脚本检查后交付简报：工程、时长、删减、碎片及保留风险，说明实际检查与听审限制。

脚本负责：检测、坐标换算、冻结、执行和验证。默认无声超过 **300ms** 进入候选，保留 **180ms** 气口。标为 review 的轻声／检测分歧自动保留，AI 无需逐一重复复核；用户指定某处返修时再看局部。新产生的不足 300ms 碎片仅在音频、VAD 和词级证据都支持无人声时删除，否则撤回停顿切口或要求改语义计划。用户原有短片段保留。

自然讲解；口播、屏幕、摄像头用同一切点，保持顺序、变速、已有缩放、布局、音量，不另编排画面，不开启抠像。

## 调用

```bash
SKILL="<本 skill 所在目录>"
bash "$SKILL/scripts/run.sh" prepare --project "/path/录屏.screenstudio"
bash "$SKILL/scripts/run.sh" pauses --work-dir "/path/work" --status auto --activity-only
```

prepare 只读原工程。自动在缓存目录（默认 `~/.screenstudio-edit`，可用环境变量 `SCREENSTUDIO_EDIT_HOME` 改）中查找同源缓存；复制工程、改变文件时间及剪辑参数不应重转录。工作目录可用 `--work-dir` 指定，其他位置的缓存用 `--cache-from /path/旧工作目录`。已有词级时间戳转录用 `--transcript-json /path/转录.json` 导入一次，不猜测无指纹转录的归属。

默认 `--asr cache-only`，缓存缺失会停止。确需新转录时显式 `--asr bailian`，通过阿里云百炼 CLI `bl`（`npm install -g bailian-cli`）调用 FunASR。首次转录会把麦克风音轨上传到百炼，先告诉用户并取得同意。凭据交由 CLI 使用，禁止读取凭据文件；未授权时请用户自己运行 `bl auth login`。`--asr-profile` 指定 `bl` 配置档，`--language` 默认 `zh`。缓存不会因一次漏认就静默重新上传。

按 [计划格式](references/plan.md)填写 `plan.json`。`inspect --work-dir /path/work --start-ms 10000 --end-ms 20000` 提供局部原片词级证据。`pauses` 支持 `--candidate-id`、`--status`、`--offset`、`--limit`；返回原片／成片坐标、原因、画面活动和前后口播。

```bash
bash "$SKILL/scripts/run.sh" dry-run --work-dir "/path/work"
bash "$SKILL/scripts/run.sh" apply --work-dir "/path/work"
```

用户明确只清空白时用 `dry-run --silence-only`。重新 prepare 后按 [计划格式](references/plan.md)处理待重读发言和待复核项。apply 只执行未过期的冻结结果，不联网或重转录。

成品默认另存到源工程旁边：`<源工程名>-初剪-vN.screenstudio`，自动递增版本；用户指定目录或名称优先，用 `--output /path/初剪.screenstudio`。报告只放工作目录 `applied.json`，成品旁不额外散放报告。在 Screen Studio 打开副本精修。

初剪完成后默认继续做指示文字缩放（用户明确只改切点／不要缩放时跳过）：

```bash
bash "$SKILL/scripts/run.sh" zoom-prepare --project "/path/初剪.screenstudio" --work-dir "/path/work"
bash "$SKILL/scripts/run.sh" zooms --work-dir "/path/work"
bash "$SKILL/scripts/run.sh" zoom-apply --work-dir "/path/work"
```

本机分析鼠标新移入后停留／左右指示、稀疏抽帧和 Apple Vision OCR，复用同源口播缓存；只为稳定画面、口播支持且整句放得下的候选自动添加 **2 倍原生固定目标缩放**。已有缩放优先，切点与时长不变。AI 只看 `zooms` 的待复核摘要，必要时看对应单帧；拿不准保留原画面，不逐帧读整片。执行和复核细节见 [文字缩放](references/zooms.md)。交付 `zoom-apply` 返回的最终工程，并说明新增／待复核数量；若无候选则沿用初剪。

需要基础 MP4 才调用 `preview --project /path/初剪.screenstudio --output /path/审片.mp4`；可用 `--start-ms/--end-ms` 选成片范围。普通速度直接拼接，只对实际变速做时间拉伸。基础预览不精确复现原生缩放、光标、样式与音频预设，不能把波形或播放画面检查说成实际听审。

支持 Screen Studio 4 单场景、单录制会话且保留原片顺序：麦克风＋屏幕＋可选摄像头。独立配音、未覆盖结构、未静音的系统／外部音轨明确停止。补录合并、其他 ASR 后端、PPT 换屏、字幕、发布不在本入口范围。默认读摘要和局部，不反复加载词级全文或重渲染整片；结束辅助进程。
