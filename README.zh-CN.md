# venv-repair

**原地修复被改名或移动过的 Windows `venv` —— 不用重装任何一个包。**

<sub>[English](README.md) · [简体中文](README.zh-CN.md)</sub>

如果你干过这件事：

```bat
ren E:\jupyyer jupyter
```

然后发现 `venv\Scripts` 里的 `pip.exe`、`jupyter.exe` 全都**毫无反应** —— 没有报错、
没有输出，退出码 1 —— 这个工具能在一秒左右修好。

```console
> venv-repair E:\jupyter\venv --dry-run
old path : E:\jupyyer\venv
new path : E:\jupyter\venv
length   : 15 -> 15 characters (15 -> 15 bytes)

  [ ok ] pip.exe                            1 occurrence(s)
  [ ok ] jupyter-lab.exe                    1 occurrence(s)
  ...
patched 40 file(s), 0 unrepairable

dry run: no file was modified.
```

## 问题出在哪

Windows 上的命令行脚本不是普通程序。pip 把它做成「启动器桩 + 写着**绝对路径**的
shebang 行 + 内嵌 zip」三段结构：

```
[ NUL 填充区 ][ #!E:\jupyyer\venv\Scripts\python.exe\n ][ 内嵌 ZIP ]
```

这个路径在安装脚本的那一刻就被写死进去了。venv 文件夹一改名或移动，所有启动器
都指向一个已经不存在的目录。

有两个细节让它特别难排查：

- **失败是静默的。** 启动器退出码 1，但不打印任何东西，看起来像工具坏了而不是路径错了。
- **`python.exe` 是好的。** 它靠相对位置的 `pyvenv.cfg` 定位自己，所以
  `python -m pip install ...` 还能用，而 `pip install ...` 不能用。这也是环境坏掉时
  你的备用通道。

## 安装

不用安装 —— 单文件、零依赖：

```console
> curl -O https://raw.githubusercontent.com/Suzuka-sama/venv-repair/main/venv_repair.py
> python venv_repair.py --help
```

或者装成命令行工具：

```console
> pip install git+https://github.com/Suzuka-sama/venv-repair
> venv-repair --help
```

需要 Python 3.9+ 和 Windows。

## 用法

```console
venv-repair <venv目录> [--old <旧路径>] [--dry-run] [--no-backup] [--verify] [--quiet]
```

| 参数 | 说明 |
|---|---|
| `<venv目录>` | venv 目录，填**当前**路径 |
| `--old PATH` | 旧路径，省略时自动探测 |
| `--dry-run` | 只报告不写入 |
| `--no-backup` | 跳过备份（默认会把 `Scripts` 备份到 `<venv>_Scripts_backup`） |
| `--verify` | 修完真的跑一次 `pip.exe --version` 来证明修好了 |
| `--quiet` | 只输出错误 |

退出码：`0` 修好 · `1` 有文件修不了（或 `--verify` 失败）· `2` 目标不是 venv ·
`3` 探测到多个旧路径，请显式传 `--old`。

推荐流程：

```console
> venv-repair E:\jupyter\venv --dry-run          # 1. 先看它会做什么
> venv-repair E:\jupyter\venv --verify           # 2. 执行，并当场验证
```

## 原理

`venv-repair` **原地**改写被写死的路径。它不重装任何包，也**从不改变启动器文件的
大小** —— 内嵌 zip 保持原来的偏移量，PE 镜像部分逐字节不动。

三种策略，全部保证长度不变：

| 新路径相对旧路径 | 做法 |
|---|---|
| 等长 | 直接等长字节替换 |
| 变长 | 向左占用 shebang 前的 NUL 填充区，行尾 `\n` 位置不动 |
| 变短 | 行内补空格撑到原长度（启动器解析时会忽略行尾空格） |

同时也会改写 `pyvenv.cfg` 和 `Scripts\` 下的文本脚本（`activate`、`activate.bat`、
`deactivate.bat` 等）。

如果路径变长到超过可用填充区能吸收的字节数，该文件会被**原样留下并报为不可修复**，
而不是靠猜硬改。这种情况下用下面的备用方案。

### 几个关键细节

- 路径的编码是**按 venv 实际探测**的，绝不假设。pip 23.1+（Python 3.12/3.13 自带的
  启动器）把 shebang 写成 UTF-8，更早的 distlib 启动器用的是 ANSI 代码页。猜错正是
  "修完还是坏的"原因，所以工具会拿旧路径去实际字节里搜，据此决定用哪种编码。
  非 ASCII 目录（如 `E:\项目\中文目录\venv`）两种情况都能处理。
- **认得出 8.3 短名**。Windows 上同一目录有两种合法写法（`C:\Users\RUNNER~1\...` 与
  长名），而 pip 和 `venv` 模块在同一个 venv 里会各写一种：pip 给启动器写的是解析后的
  路径，`venv` 往 `pyvenv.cfg` 里写的是传进来的写法。工具会同时读取两处、按**目录身份**
  而不是字符串来比较，并把找到的每种写法都修掉。没有这一步，放在短名目录下的 venv 只会
  被修好文本文件，所有 `.exe` 依旧坏着。
- `pip.exe` 的 shebang 行尾后面多一个 CRLF，识别时会容忍，而不是假设 zip 紧贴着开始。
- shebang 实际内容是 `#!<venv根>\Scripts\python.exe`，不只是 venv 根路径 —— 只按根路径
  判断会把正常文件误报成损坏。
- 文件通过临时文件加 `os.replace` 原子写入，保持原有属性。

## 安全性

- 先用 **`--dry-run`**，只报告不写入。
- **自动备份**：任何写入前把 `Scripts` 复制到 `<venv>_Scripts_backup`；只有新备份就位后
  才会删掉旧备份。
- **修后自检**：逐个重新读取启动器，比对 shebang 是否还指向旧路径，仍然坏掉的会明确列出。
- **拒绝优于破坏**：改不动的文件跳过并报告，不硬改。

## 手动验证

```console
> E:\jupyter\venv\Scripts\pip.exe --version
> E:\jupyter\venv\Scripts\jupyter.exe --version
> E:\jupyter\venv\Scripts\jupyter.exe kernelspec list
```

`pip --version` 输出里应该是**新**路径。确认没问题后，删掉 `<venv>_Scripts_backup`。

## 备用方案：重建环境

如果工具拒绝修复，或者你就是想要一个干净的环境，那就重建。只要被移动过的 venv 里
`python.exe` 还能跑，就能把包清单带过去：

```console
> E:\jupyter\venv\Scripts\python.exe -m pip freeze > %TEMP%\req.txt
> python -m venv E:\jupyter\venv
> E:\jupyter\venv\Scripts\python.exe -m pip install -r %TEMP%\req.txt
```

注意 `pip freeze` 输出里的 `-e` 可编辑安装项 —— 它们指向旧位置，需要从源码重装。

## 已有同类项目

这不是这个方向上第一个工具，也没打算是。如果你的情况和本工具的设计不匹配，
下面这些可能更合适：

| 项目 | 做法 | 说明 |
|---|---|---|
| [ci-ke/venv-fix](https://github.com/ci-ke/venv-fix) | 字节层面修补启动器，拼接 shebang | 最接近的同类；还会顺手改 `Scripts\*.py`。但它写死了 `.encode('ascii')`，遇到非 ASCII 路径会抛 `UnicodeEncodeError`；而且拼接会改变文件大小 —— 路径变长超过 NUL 填充区时可能在无警告的情况下破坏 PE 镜像 |
| [rr-info/move-venv](https://github.com/rr-info/move-venv) | 在 venv 里做字符串替换 | 作者自述 *"almost GUARANTEED TO FAIL"*，且依赖外部 `cp`、`file` 命令 |
| [hsupu/fix_entrypoints.py](https://gist.github.com/hsupu/feacdda135332d847bd5e3ccaa3ee351) | 借用 pip 内置的 `distlib` 重新生成启动器 | 前提是 venv 里的 pip 仍然可导入 |
| [WildinFree/VenvRepath](https://github.com/WildinFree/VenvRepath) | GUI，在任意项目文件里替换旧路径 | 范围更宽，不针对启动器内部结构 |
| `python -m venv --upgrade <目录>` | 原地重建环境 | 不会处理第三方包留下的 `Scripts\*.exe` 启动器 |

`venv-repair` 多出来的部分：严格保长修补、支持非 ASCII 路径、认得出 8.3 短名（按目录身份
而非字符串比较）、`--dry-run`、自动备份，以及一个明确告诉你文件是不是还坏着的修后自检。

## 局限

- 只处理标准库 `python -m venv` 创建的 venv。Poetry、Conda、`virtualenv` 的目录结构不同。
- 只支持 Windows。POSIX 系统上修复就是改一行 shebang，而且那边的 `venv` 同样不可重定位 ——
  直接重建更省事。
- 如果 venv 被移动过不止一次，不同文件里的旧路径可能不一致，这时请显式传 `--old`。
- `Lib\site-packages\**\__pycache__\*.pyc` 和 `*.dist-info` 里可能残留旧路径。这是无害的，
  工具刻意不动它们。

## 开发

```console
> python -m unittest discover -s tests -v
```

测试会创建真实的虚拟环境、移动它们，然后断言 `pip.exe` 真能跑起来 —— 包括断言没有任何
文件大小发生变化。CI 在 Windows 上覆盖 Python 3.9 / 3.11 / 3.13。

## 许可证

MIT，见 [LICENSE](LICENSE)。
