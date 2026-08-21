# FishBar 摸鱼条

FishBar 是一个轻量的 Windows 桌面 TXT 小说阅读器。阅读面板可以停靠在屏幕底部或任务栏附近：鼠标移入时显示，移开后自动隐藏，不抢占当前窗口焦点。

> **AI 制作说明**：本项目由用户提出需求，并由 AI（OpenAI Codex）协助完成界面设计、程序开发、测试优化和文档整理。

## 主要功能

- 翻页阅读与无级像素滚动
- 自动滚动及 1–120 像素/秒速度调节
- 多本小说书库切换，每本书独立保存阅读位置
- 自动识别常见中英文章节标题，支持搜索目录并一键跳转
- 根据窗口尺寸、字体和字号自动计算完整显示行
- 连续可调字体粗细、正文颜色和背景透明度
- 导入 `.ttf`、`.otf`、`.ttc` 字体，不写入系统字体目录
- 透明文字与半透明背景两种显示模式
- UTF-8、UTF-16、GB18030、GBK、Big5 文本编码支持
- Windows 通知区域图标与全局快捷键

## 下载

不想安装 Python 时，可前往仓库的 **Releases** 页面下载最新 `FishBar.exe`。

程序不会复制或上传小说文件。设置和每本书的阅读进度保存在：

```text
%APPDATA%\FishBar
```

## 从源码运行

需要 Windows 10/11 和 Python 3.11 或更高版本。

```powershell
git clone https://github.com/tqjdct/FishBar.git
cd FishBar
python -m pip install -r requirements.txt
python fishbar.py
```

也可以双击 `run_fishbar.bat`。运行示例小说：

```powershell
python fishbar.py --demo
```

Pillow 不可用时程序仍可使用 Tk 原生渲染，但透明文字的抗锯齿效果会降低。

## 快捷键

- `Ctrl + Alt + O`：导入 TXT 小说并加入书库
- `Ctrl + Alt + H`：显示或隐藏阅读面板
- `Ctrl + Alt + ← / →`：翻页或滚动
- `Ctrl + Alt + S`：打开设置
- `Ctrl + Alt + C`：打开当前小说的章节目录
- `Ctrl + Alt + R`：重置面板位置
- 鼠标滚轮：翻页或无级滚动

程序启动后会常驻 Windows 通知区域。单击托盘图标可显示阅读面板，右键可导入小说、打开设置或退出。

## 打包 Windows EXE

安装依赖后执行：

```powershell
python -m PyInstaller FishBar.spec
```

生成结果位于 `dist\FishBar.exe`。

打包配置会依次查找：

1. 项目目录中的 `NotoSerifSC-VF.ttf`
2. `C:\Windows\Fonts\NotoSerifSC-VF.ttf`

如果本机没有该字体，可从 [Google Fonts 的 Noto Serif SC 页面](https://fonts.google.com/noto/specimen/Noto+Serif+SC)获取，并将变量字体文件命名为 `NotoSerifSC-VF.ttf` 放到项目目录。字体文件体积较大，默认不提交到 Git 仓库。

## 开源许可

FishBar 源代码采用 [MIT License](LICENSE)。

Noto Serif SC 字体采用 SIL Open Font License 1.1，详见 [NotoSerifSC-OFL.txt](NotoSerifSC-OFL.txt)。
