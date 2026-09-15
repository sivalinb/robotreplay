import argparse
import asyncio
import getpass
import json
from dataclasses import replace
from pathlib import Path

from robotreplay.config import Settings
from robotreplay.store import Store, create_user


def main():
    parser = argparse.ArgumentParser(prog="robotreplay")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "demo"):
        command = commands.add_parser(name)
        command.add_argument("--port", type=int, default=8000)
        command.add_argument("--host", default="127.0.0.1")
    init = commands.add_parser("init")
    init.add_argument("--username", default="coach")
    init.add_argument("--team", help="Reuse an existing team ID only for an authorized mentor")
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--output", type=Path, default=Path("artifacts/policy-evaluation.json"))
    provider_eval = commands.add_parser("provider-evaluate")
    provider_eval.add_argument("--execute", action="store_true")
    provider_eval.add_argument("--repeats", type=int, default=1)
    provider_eval.add_argument(
        "--output", type=Path, default=Path("artifacts/provider-evaluation.json")
    )
    cache_eval = commands.add_parser("cache-evaluate")
    cache_eval.add_argument("--execute", action="store_true")
    cache_eval.add_argument("--pairs", type=int, default=3)
    cache_eval.add_argument("--output", type=Path, default=Path("artifacts/cache-evaluation.json"))
    bench = commands.add_parser("benchmark")
    bench.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    bench.add_argument("--model", required=True)
    bench.add_argument("--execute", action="store_true")
    bench.add_argument("--requests", type=int, default=10)
    bench.add_argument("--concurrency", type=int, default=1)
    bench.add_argument("--max-tokens", type=int, default=128)
    bench.add_argument("--prefix-repeats", type=int, default=8)
    bench.add_argument("--max-seconds", type=float, default=120)
    bench.add_argument("--input-price", type=float, default=0, help="USD per million input tokens")
    bench.add_argument(
        "--output-price", type=float, default=0, help="USD per million output tokens"
    )
    bench.add_argument(
        "--hourly-rate", type=float, default=0, help="Actual GPU USD/hour from your account"
    )
    bench.add_argument("--budget-usd", type=float, default=0)
    bench.add_argument("--ttft-slo-ms", type=float, default=1500)
    bench.add_argument("--latency-slo-ms", type=float, default=10000)
    bench.add_argument("--output", type=Path, default=Path("artifacts/benchmark.json"))
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "init":
        password = getpass.getpass("New mentor password (at least 12 characters): ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match")
        create_user(Store(settings.data_dir), args.username, password, args.team)
        print("Mentor created. Run: robotreplay serve")
    elif args.command in {"serve", "demo"}:
        import uvicorn

        from robotreplay.app import create_app, setup_demo

        if args.command == "demo":
            if args.host not in {"127.0.0.1", "localhost"}:
                raise SystemExit("Sample mode must bind to loopback")
            settings = replace(
                settings,
                data_dir=Path(".data/demo"),
                demo=True,
                provider="local",
                model="",
                model_api_key="",
                model_api_key_file="",
                embedding_api_key="",
                embedding_api_key_file="",
                otlp_endpoint="",
            )
            setup_demo(settings)
            print(
                "Sample account: demo / robotreplay-demo. Real uploads and model calls are disabled."
            )
        if args.host not in {"127.0.0.1", "localhost"} and not settings.secure_cookies:
            raise SystemExit(
                "Remote binding requires RR_SECURE_COOKIES=true and a TLS reverse proxy"
            )
        uvicorn.run(
            create_app(settings), host=args.host, port=args.port, workers=1, access_log=False
        )
    elif args.command == "evaluate":
        from robotreplay.benchmark import save_report
        from robotreplay.evaluations import run_evaluations

        report = run_evaluations()
        save_report(report, args.output)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "total": report["total"],
                    "release_gate": report["release_gate"],
                    "output": str(args.output),
                }
            )
        )
        raise SystemExit(0 if report["release_gate"] == "pass" else 1)
    elif args.command == "provider-evaluate":
        from robotreplay.benchmark import save_report
        from robotreplay.provider_eval import evaluate_provider

        report = asyncio.run(evaluate_provider(settings, args.execute, args.repeats))
        save_report(report, args.output)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "passed": report.get("passed"),
                    "total": report["total"],
                    "output": str(args.output),
                }
            )
        )
        raise SystemExit(1 if report.get("release_gate") == "hold" else 0)
    elif args.command == "cache-evaluate":
        from robotreplay.benchmark import save_report
        from robotreplay.provider_eval import evaluate_cache

        report = asyncio.run(evaluate_cache(settings, args.execute, args.pairs))
        save_report(report, args.output)
        print(json.dumps({"status": report["status"], "output": str(args.output)}))
    elif args.command == "benchmark":
        from robotreplay.benchmark import benchmark, save_report

        report = asyncio.run(benchmark(args))
        save_report(report, args.output)
        print(json.dumps({"status": report["status"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
