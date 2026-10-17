# Screen Studio 智能初剪 · screenstudio-edit

中文 | [English](README.en.md)

这个 skill，是我正在自用且不断迭代的一套自动化剪辑 skill，是想给自己做一套真正有效的自动化剪辑，能高效优质地产出内容。

现在这一版负责第一步：在 Screen Studio 录屏上做初剪，适合讲 PPT、白板演示这类视频。之后我会加上口播视频的智能剪辑，把做内容会用到的剪辑步骤陆续整合进来。

它会完成大量重复平淡的剪辑工作：剪掉空白停顿、失败的重录和口误，智能判断切点和画面变化；另存一个还能在 Screen Studio 里继续精修的工程副本，原工程一个字节都不改。

最费工夫的活都由本机脚本完成，不经过 AI。通过音轨转文字后，让 AI 进行智能化剪辑，所以 token 用得很少。同一条录屏重复剪辑时，转写和检测结果都直接复用。

另，这是一个 Agent Skill，Claude Code、Codex 等能读 `SKILL.md` 并运行命令的 AI 助理都能用。非 Screen Studio 官方项目。

## 它会做什么

- **清空白**：超过 300 毫秒的停顿剪到只留 180 毫秒的气口。轻声的字尾、检测拿不准的地方自动保留，宁可少剪。
- **删重录**：AI 先通读整份口播转写，同一句讲了几遍，就留完整流畅的那一遍。被删的内容必须在保留部分里讲到；只出现一次的操作、数字、限定条件和提醒不删。
- **保护你的手工修改**：已经调好的缩放、变速、布局、音量原样保留，口播、屏幕、摄像头用同一个切点，音画不会错位。
- **可以接着剪**：在 Screen Studio 里改过之后再交给它，之前的判断会带过去，只复核受影响的部分，转写不用重做。

不做的事：普通 MP4 剪辑、字幕、抠像、替换 PPT 画面、补录合并、发布。

## 需要准备

- macOS 和 Screen Studio 4 的工程（`.screenstudio`），单场景、单次录制，带麦克风和屏幕录制，摄像头可有可无
- Python 3.11 或更新版本、ffmpeg：`brew install python ffmpeg`
- 语音转写用阿里云百炼的 FunASR，需要百炼 CLI：

  ```bash
  npm install -g bailian-cli
  bl auth login
  ```

  手上已经有带词级时间戳的转写，也可以直接导入，不用百炼。

## 安装

Claude Code：

```bash
git clone https://github.com/Jianing1117/screenstudio-edit.git ~/.claude/skills/screenstudio-edit
bash ~/.claude/skills/screenstudio-edit/scripts/setup.sh
```

Codex：把上面的 `~/.claude/skills` 换成 `~/.codex/skills`。其他 AI 助理放进它读取 skill 的目录即可。

`setup.sh` 会在 skill 目录里建一个独立的 Python 环境（`.venv`），只装 numpy 和 onnxruntime。以后更新用 `git pull`。

## 使用

对 AI 说：

- 「用 screenstudio-edit 剪一下这个录屏：`~/Screen Studio Projects/xxx.screenstudio`」
- 「只清空白，不删内容」
- 在 Screen Studio 里改过之后：「接着剪这个版本」

它会按四步走：

1. **准备**：只读打开工程，转写口播，检测停顿和画面活动（点击、按键、画面变化）。第一次转写前会先征得你同意。
2. **写剪辑计划**：AI 读转写稿，决定删哪些重录；有画面操作的停顿逐条看，需要留出看屏幕时间的就保留。
3. **试剪**：脚本校验计划，算出最终切点，报告剪掉多少、保留了哪些拿不准的地方。
4. **另存**：成品存在源工程旁边，名字是 `<原名>-初剪-v1.screenstudio`，再剪一次是 v2。用 Screen Studio 打开，接着精修。

AI 不会真的去听成片。交付前请自己在 Screen Studio 里完整听一遍。

## 安全与隐私

- 原工程只读；成品另存，从不覆盖已有文件。
- 只有转写这一步联网，会把麦克风音轨上传到百炼；检测和剪辑都在本机完成。
- 按键记录只读取时间，不读取按了什么。
- 不读取任何密钥或凭据文件，百炼授权由 `bl` 自己管理。
- 转写、检测结果缓存在 `~/.screenstudio-edit`（可用环境变量 `SCREENSTUDIO_EDIT_HOME` 换位置）。复制工程、改文件时间都不会让缓存失效；删掉这个目录，下次就要重新转写。

## 目前的限制

- 转写只接百炼 FunASR，目前只在中文录音上测试过。
- 需要基础预览视频时可以生成 MP4，但它不还原 Screen Studio 的缩放动画、光标和样式，只用来快速看切点。
- 工程里有独立配音轨、多段录制、未静音的系统声音时会直接停下，不会硬剪。

## 开发

结构和测试方法见 [references/development.md](references/development.md)。

## 致谢与许可

人声检测使用 [Silero VAD](https://github.com/snakers4/silero-vad)（MIT），模型文件随仓库附在 `assets/`。

本项目以 MIT 许可发布。由加宁（[@Jianing1117](https://github.com/Jianing1117)）设计和维护。
