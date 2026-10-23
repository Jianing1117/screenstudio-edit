# 开发与测试

## 结构

- `scripts/run.sh`：统一入口，调用 `workflow.py` 的子命令：prepare → pauses / inspect → dry-run → apply，另有可选的 preview。
- `scripts/core.py`：读工程、解码音频、人声检测（Silero VAD）、能量检测、画面活动、坐标换算。不含语义判断，不读凭据。
- `scripts/continuity.py`：按录制内容找可复用的缓存，工程改动后把已有判断迁移到新计划。
- `scripts/workflow.py`：各子命令；生成停顿候选、校验计划、冻结切点、另存工程。
- `scripts/zooms.py` / `scripts/local_ocr.m`：鼠标指示、本地 OCR、原生可编辑缩放；规则见 [zooms.md](zooms.md)。初剪后运行 zoom-prepare → zooms → zoom-apply。
- `scripts/preview.py`：基础审片 MP4。
- `references/plan.md`：`plan.json` 的格式和规则，AI 写计划时读。

## 测试

```bash
SKILL=<本 skill 所在目录>
"$SKILL/.venv/bin/python3" "$SKILL/scripts/test_continuity.py" --work-dir "$(mktemp -d)"
```

测试材料是脚本现场生成的合成录音和画面，不联网、不转写，几秒钟跑完。`test_continuity.py` 会先跑 `test_workflow.py`，结果汇总在工作目录的 `test-results.json`。测试期间缓存查找只在测试目录里进行，不会碰 `~/.screenstudio-edit`。

缩放测试另跑 `scripts/test_zooms.py --work-dir /path/独立测试目录`，使用 macOS 本机合成文字画面与 Apple Vision，不联网、不转写。

改到时间轴或媒体处理时，另用真实录屏的副本验证（`cp -cR 原工程.screenstudio 副本.screenstudio`），不要拿原工程试。

## 修改时守住的几条

- 切点一律用原片毫秒坐标；变速按乘法换算，不重复变速。
- 缓存和计划都有指纹，工程或证据一变就要求重新 prepare，不套用旧结果。
- 有声内容只能由 AI 在计划里写明理由和替代表述后删除，不用词表或相似度自动删。
- 检测、计划、执行各管一段，不为某一条视频加特殊分支，也不把具体路径、切点或凭据写进代码。
