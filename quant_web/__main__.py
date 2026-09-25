"""
python -m quant_web：启动网页版量化助手，并自动打开浏览器。

    python -m quant_web                  默认 http://127.0.0.1:8765 （被占用时自动换 8766~8775）
    python -m quant_web --no-browser     不自动打开浏览器
    python -m quant_web --no-scheduler   不在收盘后自动更新
    python -m quant_web etf [命令]        旧版稳健ETF命令行菜单（等同 python -m etf_quant）
"""
import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
import webbrowser

from . import __version__, config


LINE: str = "=" * 60


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")     # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # type: ignore[attr-defined]
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def already_running(host: str, port: int) -> bool:
    """该端口上是不是已经开着一个量化助手（重复双击启动时直接打开浏览器即可）"""
    import requests

    try:
        session = requests.Session()
        session.trust_env = False           # 本机地址不走系统代理
        data = session.get(f"http://{host}:{port}/api/status", timeout=1.5).json()
    except Exception:  # noqa: BLE001  任何失败都说明不是我们的服务
        return False
    return isinstance(data, dict) and "panel" in data and "phase" in data


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        print(f"请手动在浏览器打开：{url}")


def _open_when_ready(server: object, url: str, enabled: bool) -> None:
    for _ in range(300):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    else:
        return
    print(f"\n  已启动！请在浏览器中使用：{url}")
    print("  关闭本窗口或按 Ctrl+C 即可退出。\n")
    if enabled:
        open_browser(url)


def main(argv: list[str] | None = None) -> None:
    _utf8_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "etf":
        from etf_quant.cli import main as etf_main

        etf_main(argv[1:])
        return

    parser = argparse.ArgumentParser(prog="quant_web", description="量化助手（网页版）")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-scheduler", action="store_true", help="不在收盘后自动更新")
    args = parser.parse_args(argv)

    print(LINE)
    print(f"   量化助手 网页版 v{__version__}  ·  基于 VeighNa (vnpy)")
    print(f"   数据目录：{config.WORKSPACE}")
    print(LINE)

    host: str = args.host
    port: int | None = None
    for candidate in range(args.port, args.port + 11):
        if port_free(host, candidate):
            port = candidate
            break
        if already_running(host, candidate):
            url: str = f"http://{host}:{candidate}/"
            print(f"\n  量化助手已经在运行了：{url}")
            if not args.no_browser:
                print("  正在为你打开浏览器……")
                open_browser(url)
            return
    if port is None:
        print(f"\n  端口 {args.port}~{args.port + 10} 都被其他程序占用了，请关闭一些程序后重试，")
        print("  或者用 --port 指定其他端口，例如：python -m quant_web --port 9000")
        sys.exit(1)
    if port != args.port:
        print(f"  端口 {args.port} 被占用，改用 {port}。")

    if args.no_scheduler:
        os.environ["QUANT_WEB_NO_SCHEDULER"] = "1"

    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)

    print("  正在启动，请稍候……")
    import uvicorn

    from .api.server import create_app

    config.ensure_dirs()
    app = create_app()          # 读取 QUANT_WEB_NO_SCHEDULER（--no-scheduler 已设置它）
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False, timeout_graceful_shutdown=3,
    ))
    url = f"http://{host}:{port}/"
    threading.Thread(target=_open_when_ready, args=(server, url, not args.no_browser), daemon=True).start()
    if hasattr(signal, "SIGBREAK"):
        # uvicorn 优雅关闭后会重新触发收到的信号；Ctrl+Break 默认直接杀进程，这里改成和 Ctrl+C 一样
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        server.run()
    except KeyboardInterrupt:
        pass

    from .jobs import JOBS

    if JOBS.running():
        print("\n  有任务还没做完（例如下载数据），下次打开后再点一次更新即可从断点继续。")
    print("\n  量化助手已退出，欢迎下次再来。")


if __name__ == "__main__":
    main()
