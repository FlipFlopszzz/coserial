"""coserial init — 在目标目录快速创建 MCP + Preview 配置文件。

用法：
    uv run coserial init              # 当前目录（项目级 .mcp.json）
    uv run coserial init /path/to/prj # 指定目录（项目级 .mcp.json）
    uv run coserial init --global     # 全局（~/.claude.json）
"""

import json
import os
import shutil
import subprocess
from pathlib import Path


def _coserial_root() -> Path:
    """返回 coserial 项目根目录（包含 pyproject.toml 的目录）。

    开发模式：src/coserial/mcp_config_init.py → src/ → coserial-root/
    安装模式：site-packages/coserial/mcp_config_init.py → 无法向上找
              此时用 uv 的解析机制，cwd 不需要特殊处理。
    """
    # 开发模式：向上 3 级 (src/coserial/mcp_config_init.py → src → root)
    root = Path(__file__).resolve().parent.parent.parent
    if (root / "pyproject.toml").exists():
        return root
    # 安装模式：无法确定项目根，返回 None（cwd 留空，依赖 uv 自动解析）
    return None


def _generate_mcp_json(cwd: str | None) -> dict:
    """生成 .mcp.json 内容。

    开发模式（cwd 不为空）：uv run coserial-client + cwd
    安装模式（cwd 为空）：直接 coserial-client（已在 PATH）
    """
    if cwd:
        entry: dict = {
            "command": "uv",
            "args": ["run", "coserial-client"],
            "cwd": cwd,
        }
    else:
        entry = {
            "command": "coserial-client",
        }
    return {"mcpServers": {"coserial": entry}}


def _generate_launch_json(cwd: str | None = None) -> dict:
    """生成 .claude/launch.json 内容。

    开发模式：uv run coserial-preview
    安装模式：直接 coserial-preview
    """
    if cwd:
        config = {
            "name": "coserial-web-ui",
            "runtimeExecutable": "uv",
            "runtimeArgs": ["run", "coserial-preview"],
        }
    else:
        config = {
            "name": "coserial-web-ui",
            "runtimeExecutable": "coserial-preview",
            "runtimeArgs": [],
        }
    config["port"] = 37230
    config["autoPort"] = True
    return {
        "version": "0.0.1",
        "configurations": [config],
    }


def _merge_mcp_json(existing: dict, new_entry: dict) -> dict:
    """合并 .mcp.json，在 mcpServers 中追加新条目。"""
    servers = existing.setdefault("mcpServers", {})
    if "coserial" in servers:
        return existing  # 已存在，不覆盖
    servers["coserial"] = new_entry["mcpServers"]["coserial"]
    return existing


def _merge_launch_json(existing: dict, new_config: dict) -> dict:
    """合并 .claude/launch.json，在 configurations 中追加新条目。"""
    configs = existing.setdefault("configurations", [])
    for c in configs:
        if c.get("name") == "coserial-web-ui":
            return existing  # 已存在，不覆盖
    configs.append(new_config["configurations"][0])
    return existing


def _write_json(path: Path, data: dict) -> None:
    """写入 JSON 文件，2 空格缩进，确保末尾换行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict | None:
    """读取 JSON 文件，失败返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _do_global() -> int:
    """通过 claude mcp add --scope user 注册 coserial MCP 到全局。

    使用官方 CLI 命令，格式规范，自动处理审批。
    """
    # 检查 claude CLI 是否可用
    claude_cmd = shutil.which("claude")
    if claude_cmd is None:
        print("  [ERROR] 'claude' CLI not found in PATH", flush=True)
        print("  Install Claude Code first: https://docs.anthropic.com/en/docs/claude-code", flush=True)
        return 1

    root = _coserial_root()
    if root:
        # 开发模式：用 --directory 让 uv 找到 pyproject.toml
        cmd = [
            claude_cmd, "mcp", "add",
            "--scope", "user",
            "coserial",
            "--",
            "uv", "run", "--directory", str(root), "coserial-client",
        ]
    else:
        # 安装模式：coserial-client 直接在 PATH
        cmd = [
            claude_cmd, "mcp", "add",
            "--scope", "user",
            "coserial",
            "--",
            "coserial-client",
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  [ERROR] claude mcp add failed: {e}", flush=True)
        return 1

    if result.returncode == 0:
        print("  [OK]   coserial MCP added to user scope (~/.claude.json)", flush=True)
    elif "already exists" in result.stderr:
        print("  [skip] coserial MCP already in user scope", flush=True)
    else:
        print(f"  [ERROR] claude mcp add failed (exit {result.returncode})", flush=True)
        if result.stderr.strip():
            print(f"         {result.stderr.strip()}", flush=True)
        return 1

    print(flush=True)
    if root:
        print(f"coserial: {root}", flush=True)
    else:
        print("coserial: installed mode (PATH)", flush=True)
    print(flush=True)
    print("Next steps:", flush=True)
    print("  1. Restart Claude Code (or start a new session)", flush=True)
    print("  2. coserial MCP tools are now available in ALL projects", flush=True)
    print("  3. server() -> open_session() -> preview() -> preview_start('coserial-web-ui')", flush=True)

    return 0


def do_init(target_dir: str | None = None, global_scope: bool = False) -> int:
    """创建 MCP + Preview 配置文件。

    Args:
        target_dir: 目标目录（默认当前目录）
        global_scope: True 则写入 ~/.claude.json（全局），否则写入目标目录的 .mcp.json

    Returns:
        0 成功
    """
    if global_scope:
        return _do_global()

    target = Path(target_dir or ".").resolve()
    if not target.is_dir():
        print(f"Error: directory not found -- {target}", flush=True)
        return 1

    # 解析 coserial 项目根路径
    root = _coserial_root()
    cwd = str(root) if root else None

    mcp_path = target / ".mcp.json"
    launch_path = target / ".claude" / "launch.json"

    warnings = 0

    # -- .mcp.json --
    mcp_new = _generate_mcp_json(cwd)
    if mcp_path.exists():
        existing = _read_json(mcp_path)
        if existing is None:
            print(f"  [WARN] {mcp_path.name} -- parse error, skipped", flush=True)
            warnings += 1
        elif "coserial" in existing.get("mcpServers", {}):
            print(f"  [skip] {mcp_path.name} -- coserial entry exists", flush=True)
            warnings += 1
        else:
            merged = _merge_mcp_json(existing, mcp_new)
            _write_json(mcp_path, merged)
            print(f"  [OK]   {mcp_path.name} -- appended coserial entry", flush=True)
    else:
        _write_json(mcp_path, mcp_new)
        print(f"  [OK]   {mcp_path.name} -- created", flush=True)

    # -- .claude/launch.json --
    launch_new = _generate_launch_json(cwd)
    if launch_path.exists():
        existing = _read_json(launch_path)
        if existing is None:
            print(f"  [WARN] .claude/launch.json -- parse error, skipped", flush=True)
            warnings += 1
        else:
            names = [c.get("name") for c in existing.get("configurations", [])]
            if "coserial-web-ui" in names:
                print("  [skip] .claude/launch.json -- coserial-web-ui exists", flush=True)
                warnings += 1
            else:
                merged = _merge_launch_json(existing, launch_new)
                _write_json(launch_path, merged)
                print("  [OK]   .claude/launch.json -- appended coserial-web-ui", flush=True)
    else:
        _write_json(launch_path, launch_new)
        print("  [OK]   .claude/launch.json -- created", flush=True)

    # -- summary --
    print(flush=True)
    print(f"Target: {target}", flush=True)
    if cwd:
        print(f"coserial: {cwd}", flush=True)
    else:
        print("coserial: installed mode (uv auto-resolve)", flush=True)
    print(flush=True)
    print("Next steps:", flush=True)
    print("  1. Start Claude Code in this directory (claude)", flush=True)
    print("  2. Approve the coserial MCP server on first use", flush=True)
    print("  3. server() -> open_session() -> preview() -> preview_start('coserial-web-ui')", flush=True)

    return 0
