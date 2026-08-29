"""Compatibility entrypoint for fundamental screeners."""

from __future__ import annotations


def main():
    print("旧 fundamental 管道已移除。请直接运行新的策略脚本，例如：")
    print("  python fundamental_analysis/growth_value_screener.py --market HK")
    print("  python fundamental_analysis/pr_screener.py --market HK")
    print("  python fundamental_analysis/deep_value_screener.py --market HK --refine")
    print("  python fundamental_analysis/sepa_screener.py --market HK")


if __name__ == "__main__":
    main()
