"""hermes-pack: `hermes package <out.zip>` — blacklist-based migration packager.

Heavy work lives in the CLI handler only; module import must stay cheap
(hermes_cli startup imports plugin modules eagerly for CLI discovery).
"""
from . import pack_engine


def _setup(parser):
    parser.add_argument("output", help="输出 zip 文件名（相对当前目录）")
    parser.add_argument("--home", default=None,
                        help="要打包的 HERMES_HOME（默认自动解析当前实例）")
    parser.add_argument("--exclude-file", default=None,
                        help="追加黑名单文件（一行一条规则，语义同内置）")
    parser.add_argument("--no-default-excludes", action="store_true",
                        help="只用你的 --exclude-file，不带内置黑名单")
    parser.add_argument("--include-secrets", action="store_true",
                        help="默认排除的密钥/配置/记忆库（.env、config.yaml、credential*.json、*.db）也打进包")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计与报告，不写 zip")
    parser.add_argument("--store", action="store_true",
                        help="不压缩直接存（更快、更大）")
    parser.add_argument("--force", action="store_true",
                        help="gateway 在跑也照打（WAL 一致性风险自负）")


def _handler(args):
    return pack_engine.package_main(args)


def register(ctx):
    ctx.register_cli_command(
        "package",
        "Package this Hermes data home into a migration zip (blacklist excludes)",
        _setup,
        _handler,
        description="跨机搬家一键打包：剔除可再生环境与运行时残渣，默认收录一切新增内容。",
    )
