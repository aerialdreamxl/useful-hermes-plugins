# -*- coding: utf-8 -*-
"""gateway-ui 部署脚本（整体重写）——由作者亲手执行，agent 不代跑。

前置：Gateway 已停止（托盘"退出"或 hermes gateway stop）。脚本会检测，未停即拒绝。

动作（全部幂等、自动备份）：
1. 复制本目录 → Data/plugins/custom-plugins/gateway-ui/（排除 __pycache__）
2. side-env 改名 environments/gateway-fluent-gui-qt → gateway-ui-qt
   （gen-hash 不含环境名，零重装复用 venv；菜单驻留已验证此机制）
3. config.yaml 纯文本行替换（保注释）：entries key 与 enabled 列表的
   custom-plugins/gateway-fluent-gui → custom-plugins/gateway-ui；pet 块值不动
4. 姊妹 dashboard 总线指针同步：_STATE_PATH_HINT → ("cache", "gateway-ui")
5. 编译自检 + 验证清单打印

不动：旧插件目录（作者拖垃圾箱）、旧总线目录（作者处置）、任何 settings 值。
"""
import os
import py_compile
import shutil
import subprocess
import sys
import time
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent
_hh = os.environ.get("HERMES_HOME", "").strip()
if not _hh:
    print("[拒绝] 请先设置 HERMES_HOME 环境变量（Hermes 数据家目录）再跑本脚本。")
    sys.exit(2)
HOME = Path(_hh)
DST_PLUGIN = HOME / "plugins" / "custom-plugins" / "gateway-ui"
OLD_ENV = HOME / "environments" / "gateway-fluent-gui-qt"
NEW_ENV = HOME / "environments" / "gateway-ui-qt"
CONFIG = HOME / "config.yaml"
DASH_INIT = HOME / "plugins" / "custom-plugins" / "gateway-dashboard" / "__init__.py"
OLD_ID = "custom-plugins/gateway-fluent-gui"
NEW_ID = "custom-plugins/gateway-ui"
TS = time.strftime("%Y%m%d-%H%M%S")


def say(msg):
    print(msg, flush=True)


def gateway_running() -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
             "| Select-Object -ExpandProperty CommandLine"],
            capture_output=True, timeout=30)
        text = r.stdout.decode("utf-8", "replace")
        return any("gateway run" in ln or "gateway start" in ln
                   for ln in text.splitlines())
    except Exception as exc:
        say(f"[warn] 无法检测 Gateway 进程（{exc}）——请自行确认已停止再继续。")
        return False


def backup(path: Path) -> Path:
    bak = path.with_name(path.name + f".bak-gui22-{TS}")
    shutil.copy2(path, bak)
    say(f"  备份: {bak.name}")
    return bak


def step1_copy_plugin():
    if SRC.resolve() == DST_PLUGIN.resolve():
        say("[1/6] 插件已在目标位置（clone 直装），跳过复制")
        return
    say("[1/6] 复制插件 → " + str(DST_PLUGIN))
    if DST_PLUGIN.exists():
        bak = DST_PLUGIN.with_name(f"gateway-ui.bak-gui22-{TS}")
        DST_PLUGIN.rename(bak)
        say(f"  已存在旧副本 → 挪走: {bak.name}")
    shutil.copytree(SRC, DST_PLUGIN,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    say("  完成")


def step2_rename_env():
    say("[2/6] side-env 改名（零重装）")
    if NEW_ENV.exists():
        say("  目标已存在，跳过（幂等）")
        return
    if not OLD_ENV.exists():
        say("  [warn] 旧环境目录不存在——首启将全量安装依赖（1~3 分钟）")
        return
    OLD_ENV.rename(NEW_ENV)
    py = next(NEW_ENV.glob("gen-*/venv/Scripts/python.exe"), None)
    if py is None:
        say("  [ERROR] 改名后找不到 venv python！请检查 " + str(NEW_ENV))
        sys.exit(1)
    say(f"  完成: {py}")


def step3_config():
    say("[3/6] config.yaml 行替换（保值保注释）")
    text = CONFIG.read_text(encoding="utf-8")
    if NEW_ID in text and OLD_ID not in text:
        say("  已是新名，跳过（幂等）")
        return
    n_key = text.count(OLD_ID + ":")
    n_list = text.count("- " + OLD_ID)
    if n_key == 0 and n_list == 0:
        say("  未找到旧插件键：全新安装请按 README 在 config.yaml 的 plugins.enabled")
        say("  里加入 custom-plugins/gateway-ui（本步跳过，未改动 config）")
        return
    backup(CONFIG)
    text = text.replace(OLD_ID + ":", NEW_ID + ":")
    text = text.replace("- " + OLD_ID, "- " + NEW_ID)
    CONFIG.write_text(text, encoding="utf-8")
    say(f"  完成: entries key x{n_key}, enabled 列表 x{n_list}")
    for stale in ("gateway-flutter-gui", "gateway-fluent-gui"):
        if stale in text:
            say(f"  [warn] config 仍残留 '{stale}' 字样（可能是注释/死键），请人工过目")


def step4_dashboard():
    say("[4/6] 姊妹 dashboard 总线指针同步")
    if not DASH_INIT.is_file():
        say("  dashboard 插件不存在，跳过")
        return
    text = DASH_INIT.read_text(encoding="utf-8")
    if "_dashboard_state_path" in text and "_PLUGIN_ID" in text:
        say("  新版权自带总线命名空间推导（cache/<_PLUGIN_ID>/），无需指针改写，跳过")
        return
    old = '_STATE_PATH_HINT = ("cache", "gateway-fluent-gui")'
    new = '_STATE_PATH_HINT = ("cache", "gateway-ui")'
    if new in text:
        say("  已指向新总线，跳过（幂等）")
        return
    if old not in text:
        say("  [warn] 未找到预期指针行——dashboard 可能已改版，请人工核对总线路径")
        return
    backup(DASH_INIT)
    DASH_INIT.write_text(text.replace(old, new), encoding="utf-8")
    say("  完成")


def step4b_balance_providers():
    """余额气泡：出厂余额适配器播种到 <home>/balance-providers/。
    清单制（.seeded.json）：首启整套播；之后只补「清单里没有的新出厂文件」——
    用户改过的不覆盖，用户删过的不再复活（尊重删除）。"""
    say("[5/6] 余额适配器播种 → balance-providers/")
    src = SRC / "balance-providers"
    if not src.is_dir():
        say("  插件包内无 balance-providers/，跳过")
        return
    dst = HOME / "balance-providers"
    manifest = dst / ".seeded.json"
    names = sorted(f.name for f in src.glob("*.py"))
    if not dst.exists():
        shutil.copytree(src, dst)
        seeded = names
        say(f"  首次播种 {len(seeded)} 个适配器")
    else:
        try:
            seeded = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(seeded, list):
                seeded = []
        except Exception:
            # 目录在但无清单（用户手建/旧版）：现存文件一律视为已播，不覆盖
            seeded = [f.name for f in dst.glob("*.py")]
        added = 0
        for name in names:
            if name not in seeded:
                shutil.copy2(src / name, dst / name)
                seeded.append(name)
                added += 1
        say(f"  目录已存在：新增出厂适配器 {added} 个（改过/删过的不动）")
    manifest.write_text(json.dumps(sorted(set(seeded)), ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    say("  提示：balance.enabled 默认 false，需在设置页显式开启")


def step5_verify():
    say("[6/6] 编译自检")
    pys = sorted(DST_PLUGIN.rglob("*.py"))
    bad = []
    for p in pys:
        try:
            py_compile.compile(str(p), doraise=True)
        except Exception as exc:
            bad.append(f"{p.name}: {exc}")
    if bad:
        say("  [ERROR] 编译失败:\n  " + "\n  ".join(bad))
        sys.exit(1)
    say(f"  {len(pys)} 个 .py 全部编译通过")
    py = next(NEW_ENV.glob("gen-*/venv/Scripts/python.exe"), None)
    if py:
        r = subprocess.run([str(py), "-c",
                            "import PySide6, qfluentwidgets, PIL; print('imports OK')"],
                           capture_output=True, timeout=120)
        say("  side-env: " + r.stdout.decode("utf-8", "replace").strip())
        if r.returncode != 0:
            say("  [ERROR] side-env imports 失败:\n" +
                r.stderr.decode("utf-8", "replace")[-800:])
            sys.exit(1)


def main():
    say("=== gateway-ui 部署（整体重写重写版） ===")
    if gateway_running():
        say("[拒绝] Gateway 仍在运行。请先停止（托盘退出 / hermes gateway stop）再跑本脚本。")
        sys.exit(1)
    step1_copy_plugin()
    step2_rename_env()
    step3_config()
    step4_dashboard()
    step4b_balance_providers()
    step5_verify()
    say("")
    say("=== 部署完成。验证清单（起 Gateway 后逐项核对） ===")
    say(" 1. 托盘图标出现，灯=绿；agent.log 有 'gateway-ui: registered'")
    say(" 2. 托盘菜单勾选 桌宠▸→显示桌宠 → 浮窗出现在屏内右下角（透明底）")
    say(" 3. 0.5s 内 config.yaml pet.enable-pet 变 true（watcher 回写）")
    say(" 4. 重启 Gateway → 桌宠仍在（A1 根除判据：不再翻 false）")
    say(" 5. 左键单击托盘图标 → 弹出/收起余额气泡（balance.enabled 开启时），config 不变（A2 判据）")
    say(" 6. 任务管理器只有一个 tray worker 进程（A3 判据）")
    say(" 7. 挂 30 分钟托盘不消失；worker_trace.log 有 tick 期租约续写（A4 判据）")
    say(" 8. 旧插件目录 plugins/custom-plugins/gateway-fluent-gui 可拖垃圾箱")
    say(" 9. 旧总线 cache/gateway-fluent-gui/ 确认无用后可删（新总线 cache/gateway-ui/）")
    say("10. （余额气泡）设置页开 余额气泡 → 左键单击桌宠本体弹泡；箭头切平台；")
    say("    拖拽桌宠不弹泡；泡内倒计时逐秒走；关闭开关后气泡入口消失")


if __name__ == "__main__":
    main()