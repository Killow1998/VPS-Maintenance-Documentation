"""Private local configuration and SSH transport shared by the two CLIs."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / ".deployment.private.json"


@contextmanager
def ssh_session(cfg):
    """Reuse one private SSH connection for this TUI; no port forwarding."""
    if cfg.get("_demo"):
        yield cfg
        return
    with tempfile.TemporaryDirectory(prefix="cpa-ssh-", dir="/tmp") as directory:
        session = dict(cfg, _ssh_control_path=str(Path(directory) / "control"))
        try:
            yield session
        finally:
            ssh = cfg["ssh"]
            command = [ssh[0], "-S", session["_ssh_control_path"], "-O", "exit"] + ssh[1:]
            try:
                subprocess.run(command, capture_output=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass  # ControlPersist also bounds the idle lifetime.


def private_json(path, value):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".cpa-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def load_config(filename=None):
    path = Path(filename or os.environ.get("CPA_TOOLS_CONFIG", DEFAULT_CONFIG)).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("配置不存在。先运行 claude_auth.py init，或用 --config 指定部署配置。")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("部署配置权限过宽，请先 chmod 600。")
    cfg = json.loads(path.read_text())
    ssh = cfg.get("ssh")
    if not isinstance(ssh, list) or len(ssh) < 2 or not all(isinstance(x, str) for x in ssh):
        raise RuntimeError("配置 ssh 必须是参数数组，最后一项为 user@host。")
    if ssh[-1].startswith("-") or not cfg.get("observer_config"):
        raise RuntimeError("配置缺少 SSH 目标或 observer_config。")
    cfg.setdefault("observer_script", "/opt/cpa-observer/observer.py")
    cfg.setdefault("observer_unit", "cpa-observer.service")
    cfg.setdefault("model", "claude-sonnet-5-5")
    cfg["_config_path"] = str(path)
    return cfg


def remote(cfg, action, **args):
    # Only source code is in argv. All configuration and key material use stdin.
    source = (ROOT / "remote_admin.py").read_text()
    job = {k: v for k, v in cfg.items() if not k.startswith("_")}
    job.update(action=action, **args)
    python = cfg.get("remote_python", ["python3"])
    def command():
        ssh = cfg["ssh"]
        if cfg.get("_ssh_no_mux"):
            ssh = [ssh[0], "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no"] + ssh[1:]
        elif cfg.get("_ssh_control_path"):
            ssh = [ssh[0], "-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                   "-o", "ControlPath=" + cfg["_ssh_control_path"]] + ssh[1:]
        return ssh[:-1] + ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                          "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                          ssh[-1], shlex.join(python) + " -c " + shlex.quote(source)]
    try:
        result = subprocess.run(command(), input=json.dumps(job), text=True,
                                capture_output=True, timeout=220)
        # Descriptor transfer failed before a remote session/command existed.
        # Never retry arbitrary SSH errors: a mutation may already have executed.
        if result.returncode == 255 and not result.stdout and "mux_client_request_session: send fds failed" in result.stderr and not cfg.get("_ssh_no_mux"):
            cfg["_ssh_no_mux"] = True
            result = subprocess.run(command(), input=json.dumps(job), text=True,
                                    capture_output=True, timeout=220)
    except subprocess.TimeoutExpired:
        raise RuntimeError("SSH 超时；不要重复修改。先用 list/status 核实 VPS 状态。") from None
    try:
        body = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("SSH 或远端命令失败；请检查连接、主机指纹及管理员权限。") from None
    if result.returncode or not body.get("ok"):
        raise RuntimeError("远端操作失败：" + str(body.get("error", "unknown")))
    return body


def display(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def run(main):
    os.umask(0o077)
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已停止。已保存的会话和历史用量保留。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        # Never print HTTP errors, subprocess output or arbitrary exception text.
        reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("错误：" + reason, file=sys.stderr)
        raise SystemExit(1)
