"""
Helpers the image scripts share: the ESXi password rule, the connection, finding a VM, waiting for a task, hashing a file.

The ESXi password is read from the environment variable ESXI_PASSWORD and from nowhere else (a command-line argument would
land in the shell history and the process list). It is never printed or logged.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import ssl
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

PASSWORD_VARIABLE = "ESXI_PASSWORD"
HASH_CHUNK = 1024 * 1024


def password_from_env(environ: Mapping[str, str] | None = None) -> str:
    """The ESXi password from the environment. :raises SystemExit: it is not set (the message never contains a value)."""
    env = os.environ if environ is None else environ
    password = env.get(PASSWORD_VARIABLE)
    if not password:
        raise SystemExit(
            f"{PASSWORD_VARIABLE} is not set. The password is only ever read from that environment variable, "
            "never from an argument (run the script where it is loaded, e.g. from the app's .env on the testbox)."
        )
    return password


def sha256_of(path: str | Path) -> str:
    """The SHA-256 of a file as lower-case hex, read in chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def connect(host: str, user: str, port: int = 443) -> Any:
    """Opens an ESXi session (the certificate is not verified, as everywhere in this project) and closes it at exit."""
    from pyVim.connect import (
        Disconnect,
        SmartConnect,
    )  # imported late: describe_template.py needs no pyvmomi

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    instance = SmartConnect(
        host=host,
        user=user,
        pwd=password_from_env(),
        port=port,
        sslContext=context,
    )
    atexit.register(Disconnect, instance)
    return instance


def find_vm(instance: Any, name: str) -> Any | None:
    """The virtual machine with exactly this name, or None."""
    from pyVmomi import vim

    content = instance.RetrieveContent()
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        return next((vm for vm in view.view if vm.name == name), None)
    finally:
        view.Destroy()


def wait_for_task(
    task: Any, poll_seconds: float = 1.0, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Waits until a vSphere task is finished. :raises RuntimeError: it failed (with the server's own message)."""
    while True:
        state = str(task.info.state)
        if state == "success":
            return
        if state == "error":
            error = task.info.error
            raise RuntimeError(
                getattr(error, "msg", None)
                or getattr(error, "localizedMessage", None)
                or "the task failed"
            )
        sleep(poll_seconds)
