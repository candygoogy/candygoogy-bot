"""No chat text ever reaches a command line (see the invariant in system.py).

A static check over every candygoogy/*.py:
- each system.run() call may only pass literals, f-strings of allowed parts,
  the names in ALLOWED_NAMES, a configured machine's fields (machine.ip, …) and
  str(REPO_DIR) / str(machine.ssh_port);
- each ssh() call names its action as a literal from oscommands.ACTIONS, and
  system.ssh() turns that into the command only through the machine's OS table;
- every command in those tables is a literal module constant, and the Linux and
  macOS shell scripts keep the quoting rules oscommands.py describes.
Anything else fails with file:line. To allow a new constant, add it to
COMMAND_CONSTANTS and say why.
"""

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from candygoogy import oscommands

PACKAGE = Path(__file__).resolve().parent.parent / "candygoogy"
MODULES = sorted(PACKAGE.glob("*.py"))
# One test per module, named after it.
EACH_MODULE = [pytest.param(path, id=path.name) for path in MODULES]

# Validated by settings.py before the bot starts.
CONFIG_NAMES = {"SSH_KNOWN_HOSTS"}
# Fields of a configured Machine (settings.py validates each), read as machine.<field>.
MACHINE_FIELDS = {"name", "mac", "ip", "ssh_user", "ssh_key_path", "ssh_port"}
# Module constants made only of literals and CONFIG_NAMES (checked below).
COMMAND_CONSTANTS = {
    "SSH_OPTIONS",  # system.py: ssh's fixed options
    "KNOWN_HOSTS_OPTIONS",  # system.py: -o UserKnownHostsFile=<SSH_KNOWN_HOSTS>, or nothing
    "GIT_LOG_FORMAT",  # about.py
    "GIT_DIRTY_ARG",  # about.py
}
ALLOWED_NAMES = CONFIG_NAMES | COMMAND_CONSTANTS
# The per-OS commands (oscommands.py): never passed to run() directly, only
# looked up by system.ssh() from the machine's OS table.
OS_COMMAND_FIELDS = ("shutdown", "reboot", "sleep", "details", "sessions")
# Parameters a function may pass on to run() because they're checked where they
# come from: system.ssh()'s command (below) and timeout.
FORWARDED = {("system.py", "ssh"): {"command", "timeout"}}
# What other modules may import from candygoogy.system: not run itself, so every
# run() outside system.py is spelled system.run() and found by this check.
SYSTEM_IMPORTS = {"RunResult", "ssh"}


def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def is_run_call(call: ast.Call, module: str) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id == "system" and func.attr == "run"
    return isinstance(func, ast.Name) and func.id == "run" and module == "system.py"


def is_ssh_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id == "system" and func.attr == "ssh"
    return isinstance(func, ast.Name) and func.id == "ssh"


def machine_field(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "machine"
        and node.attr in MACHINE_FIELDS
    )


def allowed(node: ast.expr, names: set[str]) -> bool:
    """Whether an argument is built only from literals, `names` and machine fields."""
    if machine_field(node):
        return True
    match node:
        case ast.Constant(value=str()):
            return True
        case ast.Name(id=name):
            return name in names
        case ast.Starred(value=value):
            return allowed(value, names)
        case ast.JoinedStr(values=values):
            return all(
                allowed(v.value, names) if isinstance(v, ast.FormattedValue) else allowed(v, names)
                for v in values
            )
        case ast.Call(func=ast.Name(id="str"), args=[ast.Name(id="REPO_DIR")], keywords=[]):
            return True
        case ast.Call(func=ast.Name(id="str"), args=[arg], keywords=[]) if machine_field(arg):
            return True
        case ast.Tuple(elts=elts):
            return all(allowed(e, names) for e in elts)
        case ast.BinOp(op=ast.Add(), left=left, right=right):
            return allowed(left, names) and allowed(right, names)
        case ast.IfExp(body=body, orelse=orelse):  # the condition isn't part of the value
            return allowed(body, names) and allowed(orelse, names)
        case _:
            return False


def calls_in(node: ast.AST, function: str | None = None) -> Iterator[tuple[str | None, ast.Call]]:
    """Every call once, with the innermost function it's in (None at module level)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from calls_in(child, child.name)
            continue
        if isinstance(child, ast.Call):
            yield function, child
        yield from calls_in(child, function)


def run_violations(tree: ast.Module, module: str) -> list[str]:
    found: list[str] = []
    for function, call in calls_in(tree):
        if not is_run_call(call, module):
            continue
        names = ALLOWED_NAMES | FORWARDED.get((module, function or ""), set())
        found += [
            f"{module}:{arg.lineno}: {ast.unparse(arg)}"
            for arg in call.args
            if not allowed(arg, names)
        ]
        found += [
            f"{module}:{kw.lineno}: keyword {ast.unparse(kw)}"
            for kw in call.keywords
            if kw.arg != "timeout"
        ]
    return found


def ssh_violations(tree: ast.Module, module: str) -> list[str]:
    """ssh(machine, "<action>", timeout=…): the action is a literal from ACTIONS."""
    found: list[str] = []
    for _, call in calls_in(tree):
        if not is_ssh_call(call):
            continue
        where = f"{module}:{call.lineno}"
        match call.args:
            case [ast.Name(), ast.Constant(value=str() as action)] if action in oscommands.ACTIONS:
                pass
            case _:
                found.append(f"{where}: {ast.unparse(call)}")
        found += [f"{where}: keyword {ast.unparse(k)}" for k in call.keywords if k.arg != "timeout"]
    return found


@pytest.mark.parametrize("path", EACH_MODULE)
def test_command_lines_are_built_from_constants_only(path: Path) -> None:
    tree = parsed(path)
    assert run_violations(tree, path.name) + ssh_violations(tree, path.name) == []


def test_ssh_looks_its_command_up_in_the_os_table() -> None:
    """The one forwarded `command` in system.ssh() comes only from the machine's OS table."""
    tree = parsed(PACKAGE / "system.py")
    [ssh] = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "ssh"]
    assigned = [
        ast.unparse(n.value)
        for n in ast.walk(ssh)
        if isinstance(n, ast.Assign) and any(ast.unparse(t) == "command" for t in n.targets)
    ]
    assert assigned == ["commands_for(machine.os).command(action)"]
    params = [a.arg for a in ssh.args.args]
    assert params == ["machine", "action", "timeout"]


def test_the_programs_the_bot_runs() -> None:
    """Every program run() starts. Docker isn't one: /homelab asks its API (dockerapi.py)."""
    programs = {
        call.args[0].value
        for path in MODULES
        for _, call in calls_in(parsed(path))
        if is_run_call(call, path.name)
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    assert programs == {"apt", "git", "ip", "ping", "ssh", "systemctl"}


def test_the_check_finds_the_command_call_sites() -> None:
    """Guards the check itself: it must actually see the calls it's meant to vet."""
    seen = {
        (path.name, call.lineno)
        for path in MODULES
        for _, call in calls_in(parsed(path))
        if is_run_call(call, path.name) or is_ssh_call(call)
    }
    modules = {name for name, _ in seen}
    assert {"about.py", "homelab.py", "power.py", "probes.py", "system.py"} <= modules
    assert len(seen) >= 12


def test_the_check_rejects_chat_text() -> None:
    lines = [
        "async def f(update, machine):",
        "    text = update.message.text",
        "    await system.run('ping', text, timeout=1)",
        "    await system.run('ping', machine.os_name_from_chat, timeout=1)",
        "    await ssh(machine, text)",
        "    await ssh(machine, 'format c:')",
        "    await system.run('git', str(update), timeout=1)",
        "    await ssh(machine, 'shutdown', shell=True)",
    ]
    tree = ast.parse("\n".join(lines))
    found = run_violations(tree, "x.py") + ssh_violations(tree, "x.py")
    assert [f.split(": ", 1)[1] for f in found] == [
        "text",
        "machine.os_name_from_chat",
        "str(update)",
        "ssh(machine, text)",
        "ssh(machine, 'format c:')",
        "keyword shell=True",
    ]


def module_constants(path: Path) -> dict[str, ast.expr]:
    return {
        node.targets[0].id: node.value
        for node in parsed(path).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }


def test_command_constants_are_literals_and_config() -> None:
    defined: set[str] = set()
    for path in MODULES:
        for name, value in module_constants(path).items():
            if name in COMMAND_CONSTANTS:
                assert allowed(value, CONFIG_NAMES), f"{path.name}: {name}"
                defined.add(name)
    assert defined == COMMAND_CONSTANTS  # each one exists at module level


def test_every_os_command_is_a_literal_constant() -> None:
    constants = module_constants(PACKAGE / "oscommands.py")
    tables = [
        node
        for node in ast.walk(parsed(PACKAGE / "oscommands.py"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OsCommands"
    ]
    assert len(tables) == len(oscommands.COMMANDS) == 3
    for table in tables:
        fields = {kw.arg: kw.value for kw in table.keywords}
        for field in OS_COMMAND_FIELDS:
            value = fields[field]
            assert isinstance(value, ast.Name), f"oscommands.py:{table.lineno}: {field}"
            assert allowed(constants[value.id], set()), f"{value.id} isn't a literal"


SHELL_SCRIPTS = [
    pytest.param(name, id=name)
    for os_name in ("linux", "macos")
    for name in (f"{os_name.upper()}_DETAILS", f"{os_name.upper()}_SESSIONS")
]


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_survive_any_login_shell(name: str) -> None:
    """Any login shell runs them alike: /bin/sh -c '…' with no ', backslash, ! or
    newline inside."""
    script = cast(str, getattr(oscommands, name))
    assert script.startswith("/bin/sh -c '") and script.endswith("'")
    assert script.count("'") == 2
    for banned in ("\\", "!", "\n"):
        assert banned not in script
    assert "candygoogy-" in script.split(";")[0]  # the marker line comes first


def test_wakeonlan_is_gone() -> None:
    """Wake-on-LAN is sent from Python (wol.py); no program, no subprocess."""
    for path in MODULES:
        assert "wakeonlan" not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("path", EACH_MODULE)
def test_run_is_never_imported_by_name(path: Path) -> None:
    """Outside system.py, run() is only reachable as system.run(), which the check sees."""
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.ImportFrom) and node.module == "candygoogy.system":
            for alias in node.names:
                assert alias.name in SYSTEM_IMPORTS, f"{path.name}:{node.lineno}"
                assert alias.asname is None, f"{path.name}:{node.lineno}"


@pytest.mark.parametrize("path", EACH_MODULE)
def test_no_other_way_to_start_a_process(path: Path) -> None:
    """Only system.run() starts processes, and never through a shell."""
    source = path.read_text(encoding="utf-8")
    for banned in ("create_subprocess_shell", "os.system", "os.popen", "import subprocess"):
        assert banned not in source, f"{path.name}: {banned}"
    if path.name != "system.py":
        assert "create_subprocess_exec" not in source, path.name
