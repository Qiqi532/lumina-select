# 替换 LGPL 组件（Qt / PySide6）的说明

本文件是为满足 **GNU LGPLv3 §4(d)(0)** 而随发行包提供的说明：

> 提供安装信息（installation information），使用户能够**用修改后的版本替换 LGPL 库**
> 并重新运行本程序。

Lumina Select（光影选片助手）以 **PyInstaller onedir** 形态发行，**不使用 onefile**。
这意味着 Qt / PySide6 的动态库以**独立文件**形式存在于发行目录中，用户可以就地替换，
无需重新链接本程序的目标码。

---

## 1. 发行目录结构（关键部分）

安装后（或解压 onedir 产物后）目录大致如下：

```text
光影选片助手/
├─ 光影选片助手.exe          # 本程序入口（自有代码，Apache-2.0）
├─ NOTICE.md
├─ THIRD_PARTY_NOTICES.md
├─ LGPL-3.0.txt              # LGPLv3 全文
├─ GPL-3.0.txt               # LGPLv3 引用的 GPLv3 全文
├─ RELINKING.md              # 本文件
└─ _internal/                # PyInstaller 依赖目录
   ├─ PySide6/               # ← Qt 绑定的 Python 扩展（LGPLv3）
   ├─ shiboken6/             # ← Qt 绑定的 CPython 桥接层（LGPLv3）
   ├─ Qt6Core.dll            # ← Qt 6 运行库（LGPLv3）
   ├─ Qt6Gui.dll
   ├─ Qt6Widgets.dll
   └─ ...
```

**自有代码（`.pyc`/入口 exe）与被替换的 LGPL 库是彼此独立的文件**，因此替换 Qt 库
不会影响本程序目标码的完整性。

---

## 2. 如何获取对应版本的 Qt / PySide6 源码

本次发行使用的版本已在 `requirements.txt` / `requirements/standard.in` 中锁定：

- `pyside6 == 6.8.1`
- `pyside6-essentials == 6.8.1`
- `shiboken6 == 6.8.1`

获取途径（任选其一）：

| 组件 | 源码获取途径 |
| --- | --- |
| PySide6 / shiboken6 | PyPI 源码发行：<https://pypi.org/project/PySide6/#files>（附 `sdist`）；官方仓库：<https://code.qt.io/cgit/pyside/pyside-setup.git/> |
| Qt 6.8.1 | Qt 官方源码归档：<https://download.qt.io/official_releases/qt/6.8/6.8.1/single/>（`qt-everywhere-src-6.8.1.tar.xz`） |

Qt 官方还提供 **Qt 6.8.1 对应的 LGPL 源码包**（`qt-everywhere-src-*`），其构建依赖与
编译参数见 Qt 文档 <https://doc.qt.io/qt-6/lgpl.html>。

---

## 3. 替换步骤（示例：换成自行修改/编译的 PySide6）

1. 记下当前发行目录中的版本（见上表），下载并编译对应的 PySide6 / Qt，得到新的
   `PySide6/`、`shiboken6/` 目录与 `Qt6*.dll` 文件。
2. 关闭本程序。
3. 备份原目录，例如把 `_internal/PySide6/` 改名为 `_internal/PySide6.bak/`。
4. 把修改后的库复制到原位置：

   ```powershell
   Copy-Item -Recurse -Force <你的构建>\PySide6   "$InstallDir\_internal\PySide6"
   Copy-Item -Recurse -Force <你的构建>\shiboken6 "$InstallDir\_internal\shiboken6"
   Copy-Item -Force <你的构建>\Qt6*.dll            "$InstallDir\_internal\"
   ```

5. 重新启动 `光影选片助手.exe`。程序使用的即为替换后的库。

> 需要注意：替换库必须与 **Python 3.12 / ABI 3 / x64** 兼容，且 `PySide6` 与
> `shiboken6` 版本必须一致。**本程序不阻止用户替换**；只要替换后的库接口兼容，
> 程序即可正常运行。

---

## 4. 为什么坚持 onedir（不使用 onefile）

- onefile 会把全部依赖打进单个 exe，运行期解压到临时目录，用户**无法就地替换**
  Qt 库，且替换后的修改会被再次解压覆盖 → 不满足 LGPL 可替换性要求。
- onedir 下 Qt 库是**普通文件**，可被用户直接替换、可被反编译/重新编译流程覆盖，
  因此天然满足 LGPLv3 §4 的可替换性要求。

本约束已在构建脚本 `光影选片助手_dist.spec` 中固化（构建产物必须是目录形态），
并由 `scripts/check_release_licenses.py` 的发行门禁进行检查。

---

## 5. 无担保声明

Qt / PySide6 组件按 **LGPLv3** 提供，**不提供任何担保**，包括但不限于适销性担保与
特定用途适用性担保。LGPLv3 全文见同目录 `LGPL-3.0.txt`。
