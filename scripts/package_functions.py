#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建批处理函数代码包。

产出两份压缩包，路径与 ``infra/functions.tf`` 中
``huaweicloud_obs_bucket_object.*.source``（``local.build_dir`` 定义在
``infra/locals.tf``）逐字一致：

======================================================  ==========================
产物                                                    挂载位置
======================================================  ==========================
``infra/.build/batch-orchestrator.zip``                 编排函数的代码包
``infra/.build/batch-checks.zip``                       两个校验函数的代码包
======================================================  ==========================

压缩包布局的硬性要求（不满足即构建失败，避免上传之后才在运行期暴露）：

1. **根目录必须有 ``index.py``**：函数处理器写的是 ``index.handler`` 与
   ``index.handler_file_check``，运行时按「处理器首段 = 根目录模块名」加载，
   多一层目录就会报「找不到入口」。根目录的 ``index.py`` 只做转发，实现仍然
   留在 ``batchcore/`` 包里。
2. **包内必须带完整的 ``batchcore/`` 包**：编排函数会调用校验函数的输入判定表，
   因此两份压缩包都收录 ``orchestrator``、``checks``、``shared`` 三个子包。
3. **不收录第三方依赖与字节码**：对象存储客户端由函数运行时镜像提供，
   ``tests``、``__pycache__``、原生扩展一律不进包。
4. **构建前逐个编译源码**：语法错误在构建阶段失败，而不是在凌晨的调度里失败。

产物字节可复现：条目顺序固定、时间戳固定、权限位固定，同一份源码重复构建
得到完全相同的包，便于比对与留档。

用法::

    python scripts/package_functions.py
    python scripts/package_functions.py --only orchestrator
"""

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS_DIR = REPO_ROOT / "src" / "batchcore"
BUILD_DIR = REPO_ROOT / "infra" / ".build"
INFRA_DIR = REPO_ROOT / "infra"

ROLES = ("orchestrator", "checks")

# 处理器名与 ``infra/functions.tf`` 的 ``handler`` 属性一一对应
HANDLERS = {
    "orchestrator": ["handler"],
    "checks": ["handler_file_check", "handler_calendar_check"],
}

# 根目录 index.py 的模板：只做转发，实现留在 batchcore 包内
ENTRY_TEMPLATE = '''"""代码包入口。

部署把本函数的处理器写为 {handlers}，运行时按「处理器首段等于根目录模块名」
加载，因此本文件只做转发，实现位于 ``batchcore`` 包中。
"""

from batchcore.{package}.index import {names}

__all__ = [{quoted}]
'''

ARCHIVES = {
    "orchestrator": "batch-orchestrator.zip",
    "checks": "batch-checks.zip",
}

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

REQUIRED_PACKAGE_MODULES = {
    "batchcore/__init__.py",
    "batchcore/orchestrator/__init__.py",
    "batchcore/orchestrator/index.py",
    "batchcore/orchestrator/plan.py",
    "batchcore/orchestrator/state.py",
    "batchcore/orchestrator/events.py",
    "batchcore/checks/__init__.py",
    "batchcore/checks/index.py",
    "batchcore/checks/file_check.py",
    "batchcore/checks/calendar_check.py",
    "batchcore/shared/__init__.py",
    "batchcore/shared/config.py",
    "batchcore/shared/credentials.py",
    "batchcore/shared/obs_store.py",
    "batchcore/shared/logs.py",
    "batchcore/shared/hwc/__init__.py",
    "batchcore/shared/hwc/sign.py",
    "batchcore/shared/hwc/http.py",
    "batchcore/shared/hwc/services.py",
}

FORBIDDEN_NAMES = {"tests", "__pycache__", "build", "node_modules"}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib", ".zip")


class BuildError(Exception):
    """构建前置校验失败。"""


# ----------------------------------------------------------------------
# 源码收集
# ----------------------------------------------------------------------
def collect_sources():
    """``batchcore`` 包内需要进包的 ``(压缩包内路径, 源文件路径)``，按字典序。"""
    if not FUNCTIONS_DIR.is_dir():
        raise BuildError("函数源码目录不存在：%s" % FUNCTIONS_DIR)

    pairs = []
    for path in sorted(FUNCTIONS_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(FUNCTIONS_DIR).as_posix()
        parts = relative.split("/")
        if any(part in FORBIDDEN_NAMES for part in parts):
            continue
        if path.suffix in FORBIDDEN_SUFFIXES:
            continue
        if path.name.startswith((".", "#")) or path.name.endswith((".tmp", ".bak", ".orig")):
            continue
        if path.suffix != ".py":
            continue
        pairs.append(("batchcore/%s" % relative, path))
    if not pairs:
        raise BuildError("函数源码目录内没有任何 Python 源码：%s" % FUNCTIONS_DIR)
    return pairs


def entry_source(role):
    """根目录 index.py 的内容。"""
    package = role
    names = HANDLERS[role]
    return ENTRY_TEMPLATE.format(
        package=package,
        handlers=" 与 ".join("index.%s" % name for name in names),
        names=", ".join(sorted(names)),
        quoted=", ".join('"%s"' % name for name in sorted(names)),
    )


def verify_python_syntax(pairs):
    for archive_path, source_path in pairs:
        try:
            compile(source_path.read_text(encoding="utf-8"), archive_path, "exec")
        except SyntaxError as error:
            raise BuildError(
                "语法错误：%s（行 %s）%s" % (source_path, error.lineno, error.msg)
            )
    for name in HANDLERS:
        compile(entry_source(name), "index.py", "exec")


def verify_handlers_are_defined(pairs):
    """处理器名必须在包内实现中存在，否则函数一调用就报「找不到方法」。"""
    sources = {archive_path: path for archive_path, path in pairs}
    for role, names in HANDLERS.items():
        module = sources.get("batchcore/%s/index.py" % role)
        if module is None:
            raise BuildError("包内缺少 %s 的实现模块" % role)
        text = module.read_text(encoding="utf-8")
        for name in names:
            if not re.search(r"^def %s\(" % re.escape(name), text, re.MULTILINE):
                raise BuildError("函数 %s 中未定义处理器 index.%s" % (role, name))


def verify_package_entries(names):
    """包内条目：根目录 index.py + batchcore 包，不允许出现别的东西。"""
    if "index.py" not in names:
        raise BuildError("包内根目录缺少 index.py，函数处理器无法加载")

    for archive_path in names:
        top = archive_path.split("/", 1)[0]
        if archive_path != "index.py" and top != "batchcore":
            raise BuildError("包内出现未预期的条目：%s" % archive_path)
        if any(part in FORBIDDEN_NAMES for part in archive_path.split("/")):
            raise BuildError("禁止把测试或缓存打进包：%s" % archive_path)
        if archive_path.endswith(FORBIDDEN_SUFFIXES):
            raise BuildError("禁止打包字节码或原生扩展：%s" % archive_path)

    missing = sorted(REQUIRED_PACKAGE_MODULES - set(names))
    if missing:
        raise BuildError("包内缺少模块：%s" % ", ".join(missing))


# ----------------------------------------------------------------------
# 与部署声明对齐
# ----------------------------------------------------------------------
def declared_sources():
    """从 ``infra`` 读出部署期望的包路径：{角色: 仓库相对路径}。

    ``source = "${local.build_dir}/batch-orchestrator.zip"``，其中
    ``build_dir = "${path.module}/.build"``，``path.module`` 即 ``infra/``。
    """
    functions_tf = (INFRA_DIR / "functions.tf").read_text(encoding="utf-8")
    locals_tf = (INFRA_DIR / "locals.tf").read_text(encoding="utf-8")

    build_dir = re.search(r'build_dir\s*=\s*"\$\{path\.module\}(/[^"]*)"', locals_tf)
    if not build_dir:
        raise BuildError("未能从 infra/locals.tf 读出 local.build_dir 的定义")
    prefix = "infra%s" % build_dir.group(1)

    found = {}
    for name, body in re.findall(
            r'resource\s+"huaweicloud_obs_bucket_object"\s+"(\w+)"\s*\{(.*?)\n\}',
            functions_tf, re.MULTILINE | re.DOTALL):
        source = re.search(r'source\s*=\s*"\$\{local\.build_dir\}(/[^"]*)"', body)
        if source:
            found[name] = "%s%s" % (prefix, source.group(1))
    if not found:
        raise BuildError("未能从 infra/functions.tf 读出 obs_bucket_object 的 source")
    return found


def verify_against_infra(role, output):
    """产物路径必须与部署声明逐字符一致。"""
    expected = declared_sources()
    relative = output.relative_to(REPO_ROOT).as_posix()
    matched = [key for key, value in expected.items() if role in key or key.startswith(role)]
    for key in matched:
        if expected[key] != relative:
            raise BuildError(
                "产物路径 %s 与部署声明 %s 不一致" % (relative, expected[key])
            )
    if not matched:
        raise BuildError("infra 中未声明 %s 的代码包对象" % role)
    return expected[matched[0]]


# ----------------------------------------------------------------------
# 打包
# ----------------------------------------------------------------------
def build(role, pairs, output):
    verify_package_entries([archive_path for archive_path, _ in pairs] + ["index.py"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("index.py", date_time=ZIP_TIMESTAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o644 & 0xFFFF) << 16
        archive.writestr(info, entry_source(role).encode("utf-8"))
        # The runtime unpacks the archive entry by entry and only creates a
        # directory when the archive states it explicitly. Writing bare file
        # entries leaves "batchcore/" absent, and importing the package then
        # fails with "No module named 'batchcore'", so every intermediate
        # directory is emitted as its own entry. The DOS directory bit (0x10)
        # must stay clear - entries carrying it are dropped during unpacking.
        emitted_dirs = set()
        for archive_path, source_path in pairs:
            parts = archive_path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                directory = "/".join(parts[:depth]) + "/"
                if directory in emitted_dirs:
                    continue
                emitted_dirs.add(directory)
                entry = zipfile.ZipInfo(directory, date_time=ZIP_TIMESTAMP)
                entry.compress_type = zipfile.ZIP_STORED
                entry.external_attr = (0o755 & 0xFFFF) << 16
                archive.writestr(entry, b"")
            member = zipfile.ZipInfo(archive_path, date_time=ZIP_TIMESTAMP)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(member, source_path.read_bytes())
    return output


def sha256_prefix(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()[:12]


def human(size):
    return "%d B" % size if size < 1024 else "%.1f KiB" % (size / 1024.0)


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建批处理函数代码包")
    parser.add_argument("--only", nargs="*", choices=ROLES, help="只构建指定的函数包")
    options = parser.parse_args(argv)

    try:
        pairs = collect_sources()
        verify_python_syntax(pairs)
        verify_handlers_are_defined(pairs)
        rows = []
        for role in options.only or ROLES:
            output = BUILD_DIR / ARCHIVES[role]
            build(role, pairs, output)
            declared = verify_against_infra(role, output)
            entries = len(pairs) + 1
            rows.append((role, declared, entries, human(output.stat().st_size),
                         sha256_prefix(output)))
    except BuildError as error:
        print("构建失败：%s" % error)
        return 1

    print("函数代码包构建完成：")
    for role, declared, entries, size, digest in rows:
        print("  %-13s %-40s %2d 个条目  %9s  sha256 %s" % (role, declared, entries, size, digest))
    print("根目录入口：index.py（处理器名见 infra/functions.tf）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
