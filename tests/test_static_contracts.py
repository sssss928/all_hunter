from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PLATFORMS = SRC / "platforms"

EXPECTED_PLATFORMS = {
    "cityline",
    "facebook",
    "famiticket",
    "fansigo",
    "funone",
    "hkticketing",
    "ibon",
    "kham",
    "kktix",
    "nolworld",
    "ticketplus",
    "tixcraft",
}


def python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def top_level_symbols(tree: ast.Module) -> set[str]:
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                symbols.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols.add(node.target.id)
    return symbols


def declared_exports(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        return list(value)
    return []


def dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_all_python_files_compile() -> None:
    for path in python_files():
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")


def test_all_exports_exist() -> None:
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        missing = sorted(set(declared_exports(tree)) - top_level_symbols(tree))
        assert not missing, f"{path.relative_to(ROOT)} exports undefined names: {missing}"


def test_async_functions_do_not_use_blocking_sleep() -> None:
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            blocking_lines = [
                child.lineno
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and dotted_name(child.func) == "time.sleep"
            ]
            assert not blocking_lines, (
                f"{path.relative_to(ROOT)}:{node.lineno} async function "
                f"{node.name!r} calls time.sleep at {blocking_lines}"
            )


def test_platform_module_set_is_complete() -> None:
    actual = {
        path.stem
        for path in PLATFORMS.glob("*.py")
        if path.name not in {"__init__.py", "registry.py"}
    }
    assert actual == EXPECTED_PLATFORMS


def test_no_truncated_implementation_markers() -> None:
    forbidden = ("因篇幅限制", "可以直接從第一份代碼中複製完整的實現")
    for path in python_files():
        source = path.read_text(encoding="utf-8")
        assert not any(marker in source for marker in forbidden), path.relative_to(ROOT)


def test_settings_static_files_use_application_root() -> None:
    source = (SRC / "settings.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = {
        target.id: ast.unparse(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assignments.get("SCRIPT_DIR") == "util.get_app_root()"


def test_example_settings_are_safe_to_publish() -> None:
    settings = json.loads(
        (ROOT / "settings.example.json").read_text(encoding="utf-8")
    )
    accounts = settings["accounts"]
    assert accounts["nolworld_account"] == ""
    assert accounts["nolworld_password"] == ""
    assert settings["ocr_captcha"]["force_submit"] is False
    assert settings["nolworld"]["security_handoff"] is True
    assert settings["advanced"]["discord_webhook_url"] == ""
    assert settings["advanced"]["telegram_bot_token"] == ""


def test_release_workflow_publishes_binary_and_source_archives() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    assert "build_release.ps1" in workflow
    assert "build_source.ps1" in workflow
    assert "dist/release/" in workflow
    assert "dist/source/" in workflow


def test_runtime_does_not_contain_protected_verification_bypass() -> None:
    runtime_files = [
        SRC / "nodriver_common.py",
        SRC / "nodriver_tixcraft.py",
        PLATFORMS / "nolworld.py",
    ]
    forbidden = (
        "turnstile-callback",
        "disable-web-security",
        "CLOUDFLARE_BYPASS_MODE",
    )
    for path in runtime_files:
        source = path.read_text(encoding="utf-8")
        assert not any(marker in source for marker in forbidden), path.name


def test_tixcraft_manual_captcha_can_submit_after_user_finishes_input() -> None:
    source = (PLATFORMS / "tixcraft.py").read_text(encoding="utf-8")
    assert "_nodriver_tixcraft_submit_captcha_if_ready" in source
    assert 'source="manual"' in source
    assert "captchaValue.length === 4" in source
    assert "form.requestSubmit" in source
    assert "button.classList.contains('btn-primary')" in source
